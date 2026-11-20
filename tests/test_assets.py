import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runtime.assets import (dependency_paths, generated_cached_report, prepare,
                            prepare_generated, prepare_online, _glb_document, _visual_stats, _material_prepare, _cache_valid, _validate_gltf_materials, assets)


class ListValues(list):
    def tolist(self):
        return list(self)


def glb(document=None):
    document = document or {'asset': {'version': '2.0'}, 'meshes': [{'primitives': []}]}
    content = json.dumps(document).encode()
    content += b' ' * (-len(content) % 4)
    return struct.pack('<4sIIII', b'glTF', 2, len(content) + 20, len(content), 0x4E4F534A) + content


def stub_scene():
    mesh = SimpleNamespace(vertices=[0, 1, 2], faces=[0], visual=SimpleNamespace(kind='vertex'))
    return SimpleNamespace(geometry={'mesh': mesh}, bounds=ListValues([[0, 0, 0], [1, 1, 1]]),
                           extents=ListValues([1, 1, 1]), export=lambda **kwargs: glb())


def source_fixture(root, filename='chair.glb', content=None):
    legacy = root / 'legacy'
    source = legacy / filename
    source.parent.mkdir(parents=True)
    source.write_bytes(content if content is not None else glb())
    catalog = legacy / 'assets/ready_asset_manifest.json'
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({'records': [{'asset_id': 'chair', 'name': 'chair',
                                              'visual_mesh_path': str(source), 'up_axis': 'Y'}]}))
    return legacy, source


class AssetPreparationTests(unittest.TestCase):
    def collection_fixture(self, root, extension='usda', kind='model'):
        data, legacy = root / 'data', root / 'legacy'
        package = data / 'assets/collections/fixture/v1/object'
        (package / 'textures').mkdir(parents=True)
        entry = package / ('model.' + extension)
        entry.write_bytes(b'native source bytes, never a static mesh conversion')
        (package / 'textures/albedo.png').write_bytes(b'original texture bytes')
        document = {'schema': 'sceneagent-asset-collections-v1', 'collections': [{'id': 'fixture-v1',
            'sourcePage': 'https://example.com/fixture', 'revision': 'v1', 'libraryTier': 'preferred',
            'licenseEvidence': {'status': 'source_declaration', 'value': 'CC0'}}], 'assets': [{
            'assetId': 'collection:fixture:v1:object', 'collection': 'fixture-v1', 'name': 'oil bottle',
            'description': 'oil bottle', 'category': 'bottle', 'sourceRoot': 'current_assets',
            'packageRoot': 'collections/fixture/v1/object', 'entry': entry.name,
            'dependencyFiles': ['textures/albedo.png'], 'format': 'native_files', 'kind': kind,
            'upAxis': 'Z', 'units': 'meters', 'qualityEvidence': {'status': 'not_individually_reviewed'},
            'importRequirements': {'materials': 'MDL activation not supported'}}]}
        (data / 'assets/collections/catalog.json').write_text(json.dumps(document))
        return legacy, data, package, document['assets'][0]['assetId']

    def test_collection_native_prepare_preserves_id_directory_and_truthful_lookup(self):
        for extension, kind in (('usda', 'model'), ('xml', 'model'), ('sdf', 'model'), ('usda', 'material')):
            with self.subTest(extension=extension, kind=kind), tempfile.TemporaryDirectory() as directory:
                legacy, data, package, identifier = self.collection_fixture(Path(directory), extension, kind)
                with patch('runtime.assets._publish_model', side_effect=AssertionError('native package flattened')), \
                     patch('runtime.assets._material_prepare', side_effect=AssertionError('native material flattened')):
                    first = prepare(legacy, data, identifier)
                    second = prepare(legacy, data, identifier)
                self.assertEqual(first, second)
                self.assertEqual(first['assetId'], identifier)
                self.assertEqual(first['kind'], 'native_files')
                self.assertEqual(first['entry'], 'files/model.' + extension)
                frozen = Path(first['reportPath']).parent
                self.assertEqual((frozen / 'files/textures/albedo.png').read_bytes(),
                                 (package / 'textures/albedo.png').read_bytes())
                item = assets(legacy, root=data, asset_id=identifier)['items'][0]
                self.assertTrue(item['currentPreparation'])
                self.assertTrue(item['filesFrozen'])
                self.assertFalse(item['rigidReady'])
                self.assertFalse(item['simulationReady'])
                self.assertFalse(item['visualReady'])
                self.assertEqual(item['units'], 'meters')
                self.assertEqual(item['upAxis'], 'Z')
                self.assertEqual(item['libraryTier'], 'preferred')
                self.assertEqual(item['qualityEvidence']['status'], 'not_individually_reviewed')
                self.assertEqual(item['importRequirements']['materials'], 'MDL activation not supported')
                (data / 'library/assets-v3.sqlite').unlink()
                self.assertTrue(assets(legacy, root=data, asset_id=identifier)['items'][0]['currentPreparation'])
                (frozen / 'files/textures/albedo.png').write_bytes(b'tampered frozen texture')
                self.assertFalse(assets(legacy, root=data, asset_id=identifier)['items'][0]['currentPreparation'])
                with self.assertRaisesRegex(ValueError, 'changed|missing'):
                    prepare(legacy, data, identifier)

    def test_collection_native_dependencies_and_runtime_changes_invalidate_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, package, identifier = self.collection_fixture(Path(directory))
            with patch('runtime.assets._versions', return_value={'algorithm': 'first'}):
                first = prepare(legacy, data, identifier)
                self.assertTrue(assets(legacy, root=data, asset_id=identifier)['items'][0]['currentPreparation'])
                (package / 'textures/albedo.png').write_bytes(b'changed texture')
                self.assertFalse(assets(legacy, root=data, asset_id=identifier)['items'][0]['currentPreparation'])
                second = prepare(legacy, data, identifier)
            with patch('runtime.assets._versions', return_value={'algorithm': 'second'}):
                self.assertFalse(assets(legacy, root=data, asset_id=identifier)['items'][0]['currentPreparation'])
                third = prepare(legacy, data, identifier)
                self.assertTrue(assets(legacy, root=data, asset_id=identifier)['items'][0]['currentPreparation'])
            self.assertEqual({first['assetId'], second['assetId'], third['assetId']}, {identifier})
            self.assertEqual(len({first['digest'], second['digest'], third['digest']}), 3)
            self.assertEqual(json.loads(Path(first['reportPath']).read_text()), first)
            (package / 'textures/albedo.png').unlink()
            with self.assertRaisesRegex(ValueError, 'missing|plain file'):
                prepare(legacy, data, identifier)

    def test_collection_object_provenance_overrides_defaults_through_prepare_and_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, identifier = self.collection_fixture(Path(directory))
            catalog = data / 'assets/collections/catalog.json'
            document = json.loads(catalog.read_text())
            first = document['assets'][0]
            first.update(sourcePage='https://example.com/object-a', sourceRevision='object-a-v7',
                licenseEvidence={'status': 'object_source_declaration', 'value': 'CC-BY-4.0'})
            second = {**first, 'assetId': identifier + '-other', 'sourcePage': 'https://example.com/object-b',
                'sourceRevision': 'object-b-v8',
                'licenseEvidence': {'status': 'object_source_declaration', 'value': 'CC-BY-NC-4.0'}}
            document['assets'].append(second)
            catalog.write_text(json.dumps(document))
            for original in (first, second):
                selected = original['assetId']
                before = assets(legacy, root=data, asset_id=selected)['items'][0]
                self.assertEqual(before['licenseEvidence'], original['licenseEvidence'])
                self.assertEqual(before['sourcePage'], original['sourcePage'])
                self.assertEqual(before['sourceRevision'], original['sourceRevision'])
                self.assertEqual(before['collectionRevision'], 'v1')
                report = prepare(legacy, data, selected)
                self.assertEqual(report['source']['licenseEvidence'], original['licenseEvidence'])
                self.assertEqual(report['source']['sourcePage'], original['sourcePage'])
                self.assertEqual(report['source']['revision'], original['sourceRevision'])
                (data / 'library/assets-v3.sqlite').unlink()
                after = assets(legacy, root=data, asset_id=selected)['items'][0]
                self.assertTrue(after['currentPreparation'])
                self.assertEqual(after['licenseEvidence'], original['licenseEvidence'])
                self.assertEqual(after['sourcePage'], original['sourcePage'])
                self.assertEqual(after['sourceRevision'], original['sourceRevision'])

    def test_collection_size_fit_requires_known_axis_and_units_before_and_after_prepare(self):
        for axis, units, expected in ((None, 'meters', 'unknown'), ('unknown', 'meters', 'unknown'),
                ('Y', 'meters', 'fits'), ('Z', 'meters', 'exceeds'), ('Z', 'unknown', 'unknown')):
            with self.subTest(axis=axis, units=units), tempfile.TemporaryDirectory() as directory:
                legacy, data, _, identifier = self.collection_fixture(Path(directory))
                catalog = data / 'assets/collections/catalog.json'
                document = json.loads(catalog.read_text())
                original = document['assets'][0]
                original.update(size=[.2, .6, .3], units=units)
                if axis is None:
                    original.pop('upAxis')
                else:
                    original['upAxis'] = axis
                catalog.write_text(json.dumps(document))
                before = assets(legacy, root=data, asset_id=identifier, size=[.5, .5, 1])['items'][0]
                self.assertEqual(before['upAxis'], axis or 'unknown')
                self.assertEqual(before['sizeMatch']['status'], expected)
                prepare(legacy, data, identifier)
                after = assets(legacy, root=data, asset_id=identifier, size=[.5, .5, 1])['items'][0]
                self.assertEqual(after['sizeMatch'], before['sizeMatch'])
                self.assertEqual(after['upAxis'], before['upAxis'])

    def test_collection_structure_roles_and_measured_axes_survive_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, identifier = self.collection_fixture(Path(directory))
            catalog = data / 'assets/collections/catalog.json'
            document = json.loads(catalog.read_text())
            metadata = {'jointCount': 3, 'movableJointCount': 2, 'jointTypes': ['fixed', 'revolute', 'prismatic'],
                'jointEvidence': {'status': 'source_inspected', 'method': 'fixture_usd_joint_inspection',
                    'sourceRef': 'fixture/report.json', 'sourceRevision': 'v1', 'sourceDigest': 'a' * 64},
                'objectRole': 'whole', 'sourceRole': 'fixture', 'sourceObjectType': 'Articulation',
                'objectGroup': 'Furniture', 'technicalEvidence': {'method': 'fixture_static_source_inspection'},
                'size': [.2, .3, .8], 'sizeUnits': 'meters', 'sizeAxis': 'Z', 'sizeEvidence': 'measured_source_bounds'}
            document['assets'][0].update(metadata, units='unknown', upAxis='unknown')
            catalog.write_text(json.dumps(document))
            before = assets(legacy, root=data, asset_id=identifier, size=[1, 1, 1])['items'][0]
            for key, value in metadata.items():
                self.assertEqual(before[key], value)
            self.assertEqual(before['units'], 'unknown')
            self.assertEqual(before['upAxis'], 'unknown')
            self.assertEqual(before['sizeMatch']['status'], 'fits')
            report = prepare(legacy, data, identifier)
            (data / 'library/assets-v3.sqlite').unlink()
            after = assets(legacy, root=data, asset_id=identifier, size=[1, 1, 1])['items'][0]
            for key, value in metadata.items():
                self.assertEqual(after[key], value)
                self.assertEqual(report['inputs']['sourceMetadata'][key], value)
            self.assertTrue(after['currentPreparation'])
            self.assertFalse(after['visualReady'])
            self.assertFalse(after['simulationReady'])
            self.assertEqual(after['units'], 'unknown')
            self.assertEqual(after['upAxis'], 'unknown')

    def test_invalid_source_measurements_and_boolean_joint_counts_remain_unknown(self):
        for count, size in ((True, [True, .2, .3]), (float('nan'), [.1, float('nan'), .3]),
                            (-1, [.1, float('inf'), .3]), (2.5, [.1, -.2, .3])):
            with self.subTest(count=count, size=size), tempfile.TemporaryDirectory() as directory:
                legacy, data, _, identifier = self.collection_fixture(Path(directory))
                catalog = data / 'assets/collections/catalog.json'
                document = json.loads(catalog.read_text())
                document['assets'][0].update(movableJointCount=count, jointCount=True, size=size,
                    jointEvidence={'status': 'source_inspected'}, objectRole='invented')
                catalog.write_text(json.dumps(document))
                item = assets(legacy, root=data, asset_id=identifier)['items'][0]
                self.assertIsNone(item['movableJointCount'])
                self.assertIsNone(item['jointCount'])
                self.assertIsNone(item['size'])
                self.assertEqual(item['sizeEvidence'], 'unknown')
                self.assertEqual(item['objectRole'], 'unknown')
                required = assets(legacy, root=data, requiresJoints=True)
                self.assertEqual(required['items'], [])
                self.assertEqual(required['jointFilter']['unknown'], 1)

    def test_boolean_measurement_cannot_register_a_current_prepared_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output.glb'
            output.write_bytes(glb())
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                report = prepare_generated(root / 'data', output, {'asset.py': '# fixture'}, 'asset.py', [], name='chair')
            path = Path(report['reportPath'])
            path.write_text(json.dumps({**report, 'size': [True, 1, 1]}))
            result = assets(root / 'legacy', root=root / 'data', asset_id=report['assetId'])
            self.assertEqual(result['items'], [])

    def test_native_bundle_preserves_layout_without_loading_models_or_weights(self):
        from runtime.assets import prepare_native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'source'
            (package / 'models').mkdir(parents=True)
            (package / 'policy').mkdir()
            entry = package / 'models/arm.urdf'
            entry.write_text('<robot name="fixture"/>')
            weight = package / 'policy/model.pt'
            weight.write_bytes(b'not-executable-fixture-weights')
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                report = prepare_native(root / 'data', entry, [weight], allowed_root=package,
                    source={'kind': 'local_file', 'licenseEvidence': {'status': 'caller_declaration_unverified', 'value': 'fixture'}})
                self.assertEqual(report['kind'], 'native_files')
                self.assertEqual(report['entry'], 'files/models/arm.urdf')
                self.assertEqual(report['source']['kind'], 'local_file')
                self.assertEqual(report['checks'], ['declared_files_frozen', 'file_digests_verified'])
                self.assertNotIn('size', report)
                self.assertNotIn('visualDigest', report)
                prepared = Path(report['reportPath']).parent
                self.assertEqual((prepared / 'files/policy/model.pt').read_bytes(), weight.read_bytes())
                self.assertEqual(_cache_valid(prepared), report)
                self.assertEqual(prepare_native(root / 'data', entry, [weight], allowed_root=package,
                    source=report['source']), report)
                (prepared / 'files/policy/model.pt').write_bytes(b'changed')
                self.assertIsNone(_cache_valid(prepared))

    def test_native_package_filenames_do_not_collide_with_preparer_metadata(self):
        from runtime.assets import prepare_native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            entry = source / 'report.json'
            entry.write_text('{"nativeConfiguration":true}')
            dependency = source / 'checkpoint.tmp'
            dependency.write_bytes(b'opaque native fixture')
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                report = prepare_native(root / 'data', entry, [dependency])
                self.assertEqual(_cache_valid(Path(report['reportPath']).parent), report)
                self.assertIn('files/report.json', report['frozenDigests'])
                self.assertIn('files/checkpoint.tmp', report['frozenDigests'])

    def test_native_weights_are_hashed_and_copied_without_reading_whole_file(self):
        from runtime.assets import prepare_native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weight = root / 'model.pt'
            weight.write_bytes(b'opaque-fixture' * 4096)
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}), \
                    patch.object(Path, 'read_bytes', side_effect=AssertionError('whole-file read is not needed')):
                report = prepare_native(root / 'data', weight)
                self.assertEqual(_cache_valid(Path(report['reportPath']).parent), report)

    def test_native_resources_are_discoverable_but_not_reported_as_validated_physics(self):
        from runtime.assets import prepare_native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resource = root / 'policy.pt'
            resource.write_bytes(b'fixture weights')
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                report = prepare_native(root / 'data', resource)
                item = assets(root / 'legacy', root=root / 'data', asset_id=report['assetId'])['items'][0]
                self.assertTrue(item['filesFrozen'])
                self.assertFalse(item['rigidReady'])
                self.assertFalse(item['simulationReady'])
                self.assertFalse(item['visualReady'])
                self.assertEqual(item['measuredSize']['status'], 'unknown')

    def test_local_native_source_rejects_project_and_private_files_before_copy(self):
        from runtime.assets import prepare_local_native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / 'data/assets/public/model.usda'
            public.parent.mkdir(parents=True)
            public.write_text('#usda 1.0')
            private = root / 'data/projects/other/secrets.pt'
            private.parent.mkdir(parents=True)
            private.write_bytes(b'not public')
            with self.assertRaisesRegex(ValueError, 'public|source root'):
                prepare_local_native(root / 'legacy', root / 'data', private)
            with self.assertRaisesRegex(ValueError, 'public|source root'):
                prepare_local_native(root / 'legacy', root / 'data', public, [private])
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                report = prepare_local_native(root / 'legacy', root / 'data', public)
            self.assertEqual(report['kind'], 'native_files')
            self.assertEqual(report['source']['kind'], 'local_files')
            self.assertEqual(report['source']['licenseEvidence']['status'], 'unknown')

    def test_native_bundle_rejects_uncontained_and_aliased_paths(self):
        from runtime.assets import prepare_native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'source'
            package.mkdir()
            entry = package / 'model.usda'
            entry.write_text('#usda 1.0')
            outside = root / 'outside.pt'
            outside.write_bytes(b'fixture')
            for dependencies in ([outside], [entry]):
                with self.subTest(dependencies=dependencies), self.assertRaisesRegex(ValueError, 'root|duplicate'):
                    prepare_native(root / 'data', entry, dependencies, allowed_root=package)
            alias = package / 'alias.usda'
            alias.symlink_to(entry)
            with self.assertRaisesRegex(ValueError, 'symlink|plain'):
                prepare_native(root / 'data', alias, [], allowed_root=package)

    def test_generated_native_outputs_require_exact_declared_files_and_cache_source(self):
        from runtime.assets import prepare_generated_native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'
            output.mkdir()
            (output / 'model.usda').write_text('#usda 1.0')
            (output / 'policy.yaml').write_text('scale: 1')
            files = {'asset.py': '# SYSTEM_VERIFICATION_ONLY fixture'}
            declaration = {'entry': 'model.usda', 'files': ['model.usda', 'policy.yaml']}
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                report = prepare_generated_native(root / 'data', output, declaration, files, 'asset.py', [])
                cached = generated_cached_report(root / 'data', files, 'asset.py', [])
                self.assertEqual(cached, report)
                prepared = Path(report['reportPath']).parent
                self.assertEqual((prepared / 'provenance/source/asset.py').read_text(), files['asset.py'])
                (output / 'hidden.txt').write_text('undeclared')
                with self.assertRaisesRegex(ValueError, 'declared|output'):
                    prepare_generated_native(root / 'data', output, declaration, files, 'asset.py', [])

    def test_native_mdl_roots_freeze_only_declared_files_and_distinguish_inputs(self):
        from runtime.assets import prepare_local_native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = root / 'data/assets/vendor'
            package.mkdir(parents=True)
            entry = package / 'model.usda'; entry.write_text('#usda 1.0')
            module = package / 'mdl/probe.mdl'; module.parent.mkdir()
            module.write_text('mdl 1.7; // SYSTEM_VERIFICATION_ONLY')
            hidden = module.parent / 'undeclared.png'; hidden.write_bytes(b'not declared')
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                report = prepare_local_native(root / 'legacy', root / 'data', entry, [module],
                    mdl_search_roots=[str(module.parent)])
                wider = prepare_local_native(root / 'legacy', root / 'data', entry, [module],
                    mdl_search_roots=[str(package)])
            self.assertEqual(report['inputs']['mdlSearchRoots'], ['files/vendor/mdl'])
            self.assertNotEqual(report['digest'], wider['digest'])
            self.assertEqual(report['nativeFiles'], ['files/vendor/model.usda', 'files/vendor/mdl/probe.mdl'])
            self.assertFalse((Path(report['reportPath']).parent / 'files/vendor/mdl/undeclared.png').exists())
            self.assertEqual(_cache_valid(Path(report['reportPath']).parent), report)
            for bad in ([str(root)], [str(module.parent)] * 2, ['mdl'], [str(entry)], 'mdl'):
                with self.subTest(roots=bad), self.assertRaisesRegex(ValueError, 'MDL|root|directory'):
                    prepare_local_native(root / 'legacy', root / 'data', entry, [module], mdl_search_roots=bad)
            alias = package / 'alias'; alias.symlink_to(module.parent, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink|MDL'):
                prepare_local_native(root / 'legacy', root / 'data', entry, [module], mdl_search_roots=[str(alias)])

    def test_generated_mdl_roots_cache_reuses_unique_identity_and_keeps_variants_separate(self):
        from runtime.assets import prepare_generated_native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); output = root / 'output'; output.mkdir()
            (output / 'model.usda').write_text('#usda 1.0')
            (output / 'mdl').mkdir(); (output / 'mdl/probe.mdl').write_text('mdl 1.7;')
            files = {'asset.py': '# SYSTEM_VERIFICATION_ONLY identical source'}
            declaration = {'entry': 'model.usda', 'files': ['model.usda', 'mdl/probe.mdl'], 'mdlSearchRoots': ['mdl']}
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                report = prepare_generated_native(root / 'data', output, declaration, files, 'asset.py', [])
                self.assertEqual(report['inputs']['mdlSearchRoots'], ['files/mdl'])
                self.assertEqual(generated_cached_report(root / 'data', files, 'asset.py', []), report)
                variant = {**declaration, 'mdlSearchRoots': ['.']}
                second = prepare_generated_native(root / 'data', output, variant, files, 'asset.py', [])
                self.assertNotEqual(report['digest'], second['digest'])
                self.assertIsNone(generated_cached_report(root / 'data', files, 'asset.py', []))
                self.assertEqual(prepare_generated_native(root / 'data', output, declaration, files, 'asset.py', []), report)
                item = assets(root / 'legacy', root=root / 'data', asset_id=report['assetId'])['items'][0]
                self.assertEqual(item['id'], report['assetId'])
                (Path(report['reportPath']).parent / 'files/mdl/probe.mdl').write_text('changed')
                self.assertIsNone(_cache_valid(Path(report['reportPath']).parent))
                self.assertEqual(generated_cached_report(root / 'data', files, 'asset.py', []), second)
            for bad in (['../'], ['/outside'], ['missing'], ['mdl', 'mdl']):
                with self.subTest(roots=bad), self.assertRaisesRegex(ValueError, 'MDL|root|relative|directory'):
                    prepare_generated_native(root / 'data', output, {**declaration, 'mdlSearchRoots': bad}, files, 'asset.py', [])

    def test_collection_mdl_roots_reuse_declared_package_root(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, package, identifier = self.collection_fixture(Path(directory))
            module = package / 'material.mdl'; module.write_text('mdl 1.7;')
            catalog = data / 'assets/collections/catalog.json'
            value = json.loads(catalog.read_text()); value['assets'][0]['dependencyFiles'].append('material.mdl')
            catalog.write_text(json.dumps(value))
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                report = prepare(legacy, data, identifier)
            self.assertEqual(report['inputs']['mdlSearchRoots'], ['files'])
            (package / 'shaders').mkdir(); module.rename(package / 'shaders/material.mdl')
            value['assets'][0]['dependencyFiles'][-1] = 'shaders/material.mdl'
            value['assets'][0]['mdlSearchRoots'] = ['shaders']
            catalog.write_text(json.dumps(value))
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                changed = prepare(legacy, data, identifier)
            self.assertEqual(changed['inputs']['mdlSearchRoots'], ['files/shaders'])
            self.assertNotEqual(changed['digest'], report['digest'])

    def test_collection_sdk_mdl_reference_does_not_require_a_package_module_root(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, package, identifier = self.collection_fixture(Path(directory))
            catalog = json.loads((data / 'assets/collections/catalog.json').read_text())
            entry = package / catalog['assets'][0]['entry']
            entry.write_text('#usda 1.0\ndef Shader "Material" {\n    asset info:mdl:sourceAsset = @OmniPBR.mdl@\n}\n')
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                report = prepare(legacy, data, identifier)
            self.assertEqual(report['inputs'].get('mdlSearchRoots', []), [])
            self.assertEqual(Path(report['path']).read_text(), entry.read_text())

    def test_generated_mdl_root_cache_does_not_change_a_legacy_native_package(self):
        from runtime.assets import prepare_generated_native
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); output = root / 'output'; output.mkdir()
            (output / 'model.usda').write_text('#usda 1.0')
            (output / 'material.mdl').write_text('mdl 1.7;')
            files = {'asset.py': '# SYSTEM_VERIFICATION_ONLY legacy cache compatibility'}
            declaration = {'entry': 'model.usda', 'files': ['model.usda', 'material.mdl']}
            with patch('runtime.assets._versions', return_value={'algorithm': 'native-test'}):
                legacy = prepare_generated_native(root / 'data', output, declaration, files, 'asset.py', [])
                with_roots = prepare_generated_native(root / 'data', output,
                    {**declaration, 'mdlSearchRoots': ['.']}, files, 'asset.py', [])
                self.assertEqual(_cache_valid(Path(legacy['reportPath']).parent), legacy)
                self.assertEqual(_cache_valid(Path(with_roots['reportPath']).parent), with_roots)
                self.assertIsNone(generated_cached_report(root / 'data', files, 'asset.py', []))
                self.assertEqual(prepare_generated_native(root / 'data', output, declaration, files, 'asset.py', []), legacy)

    def test_material_sampler_scope_keeps_unused_sampler_build_compatibility(self):
        document = {'samplers': [{'wrapS': 33071}], 'textures': [{'sampler': 0}], 'materials': [{}]}
        with self.assertRaisesRegex(ValueError, 'sampler|wrap'):
            _validate_gltf_materials(document)
        _validate_gltf_materials(document, all_samplers=False)
        document['materials'][0]['normalTexture'] = {'index': 0}
        with self.assertRaisesRegex(ValueError, 'sampler|wrap'):
            _validate_gltf_materials(document, all_samplers=False)

    def test_source_texture_controls_are_rejected_before_a_loader_can_drop_them(self):
        for texture in ({'texCoord': 1}, {'scale': .3}, {'strength': .4},
                        {'extensions': {'KHR_texture_transform': {'offset': [.2, 0]}}}):
            with self.subTest(texture=texture), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                document = {'asset': {'version': '2.0'}, 'meshes': [{'primitives': [{'material': 0}]}],
                            'materials': [{'normalTexture': {'index': 0, **texture}}]}
                path = root / 'source.glb'
                path.write_bytes(glb(document))
                with self.assertRaisesRegex(ValueError, 'texture|mapping'):
                    dependency_paths(path)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.glb'
            path.write_bytes(glb({'asset': {'version': '2.0'}, 'meshes': [{'primitives': [{'material': 0}]}],
                                 'samplers': [{'wrapS': 33071}], 'textures': [{'sampler': 0}],
                                 'materials': [{'normalTexture': {'index': 0}}]}))
            with self.assertRaisesRegex(ValueError, 'sampler|wrap'):
                dependency_paths(path)

    def test_direct_prepare_of_an_old_generated_id_rebuilds_under_current_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output.glb'
            output.write_bytes(glb())
            files = {'asset.py': '# original generated source'}
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), \
                    patch('runtime.assets._versions', return_value={'algorithm': 'old'}):
                previous = prepare_generated(root / 'data', output, files, 'asset.py', [], name='chair')
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), \
                    patch('runtime.assets._versions', return_value={'algorithm': 'current'}):
                current = prepare(root / 'legacy', root / 'data', previous['assetId'])
            self.assertNotEqual(previous['digest'], current['digest'])
            self.assertEqual(current['inputs']['versions'], {'algorithm': 'current'})
            self.assertEqual(current['assetId'], previous['assetId'])
            self.assertEqual((Path(current['reportPath']).parent / 'provenance/source/asset.py').read_text(), files['asset.py'])
            self.assertEqual(json.loads(Path(previous['reportPath']).read_text()), previous)

    def test_generated_id_reprepares_from_valid_old_source_when_newer_snapshot_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output.glb'
            output.write_bytes(glb())
            files = {'asset.py': '# original generated source'}
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                with patch('runtime.assets._versions', return_value={'algorithm': 'old'}):
                    previous = prepare_generated(root / 'data', output, files, 'asset.py', [], name='chair')
                with patch('runtime.assets._versions', return_value={'algorithm': 'intermediate'}):
                    intermediate = prepare(root / 'legacy', root / 'data', previous['assetId'])
                corrupted = Path(intermediate['reportPath']).parent / 'provenance/source/asset.py'
                corrupted.write_text('# corrupted newer source')
                with patch('runtime.assets._versions', return_value={'algorithm': 'current'}):
                    current = prepare(root / 'legacy', root / 'data', previous['assetId'])
            self.assertEqual(current['assetId'], previous['assetId'])
            self.assertEqual((Path(current['reportPath']).parent / 'provenance/source/asset.py').read_text(), files['asset.py'])
            self.assertEqual(corrupted.read_text(), '# corrupted newer source')
            self.assertEqual(json.loads(Path(previous['reportPath']).read_text()), previous)

    def test_prepared_material_keeps_ao_pixels_and_declares_actual_map_channels(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            images = {'albedo': Image.new('RGBA', (7, 3), (90, 60, 30, 128)),
                      'normal': Image.new('RGB', (5, 2), (128, 128, 255)),
                      'roughness': Image.new('L', (7, 3), 63),
                      'metallic': Image.new('L', (7, 3), 191),
                      'ao': Image.new('L', (4, 2))}
            images['ao'].putdata([0, 32, 64, 96, 128, 160, 192, 255])
            for name, image in images.items():
                image.save(source / (name + '.png'))
            report = _material_prepare(root / 'data', {
                'id': 'ambientcg:Asphalt003', 'name': 'Asphalt003', 'description': 'asphalt',
                'category': 'asphalt', 'index': 'data/materials/sceneagent-material-index.json',
                'license': 'CC0', 'maps': {name: str(source / (name + '.png')) for name in images}})
            prepared = Path(report['reportPath']).parent
            self.assertIn('occlusion', report['maps'])
            with Image.open(prepared / report['maps']['occlusion']) as image:
                self.assertEqual(image.size, (4, 2))
                self.assertEqual(image.tobytes(), images['ao'].tobytes())
            evidence = {image['role']: image for image in report['textureEvidence']['images']}
            expected = {'baseColor': ('sRGB', ['R', 'G', 'B', 'A'], (7, 3)),
                        'normal': ('linear', ['R', 'G', 'B'], (5, 2)),
                        'metallicRoughness': ('linear', ['R', 'G', 'B'], (7, 3)),
                        'occlusion': ('linear', ['L'], (4, 2))}
            for role, (color_space, channels, size) in expected.items():
                with self.subTest(role=role), Image.open(prepared / report['maps'][role]) as image:
                    self.assertEqual((evidence[role]['width'], evidence[role]['height']), size)
                    self.assertEqual(evidence[role]['channels'], channels)
                    self.assertEqual(evidence[role]['colorSpace'], color_space)
                    self.assertEqual(evidence[role]['mode'], image.mode)
                    self.assertEqual(evidence[role]['sha256'], hashlib.sha256(
                        (prepared / report['maps'][role]).read_bytes()).hexdigest())
                    self.assertEqual(evidence[role]['pixelDigest'], hashlib.sha256(image.tobytes()).hexdigest())
            self.assertEqual(evidence['metallicRoughness']['channelUsage'], {'roughness': 'G', 'metallic': 'B'})
            self.assertEqual(evidence['occlusion']['channelUsage'], {'occlusion': 'R'})
            with Image.open(prepared / report['maps']['metallicRoughness']) as image:
                self.assertEqual(image.getpixel((0, 0)), (255, 63, 191))

    def test_scalar_source_maps_use_red_data_channel_without_luminance_mixing(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            maps = {}
            for name, color in [('albedo', (100, 200, 50)), ('ao', (32, 240, 128)),
                                ('roughness', (64, 200, 16)), ('metallic', (192, 10, 64))]:
                path = root / (name+'.png')
                Image.new('RGB', (3, 2), color).save(path)
                maps[name] = str(path)
            report = _material_prepare(root / 'data', {'id': 'ambientcg:scalar-test', 'name': 'scalar',
                'description': 'scalar channel fixture', 'category': '', 'index': 'fixture', 'license': 'CC0', 'maps': maps})
            prepared = Path(report['reportPath']).parent
            with Image.open(prepared / report['maps']['occlusion']) as image:
                self.assertEqual(image.getpixel((0, 0)), 32)
            with Image.open(prepared / report['maps']['metallicRoughness']) as image:
                self.assertEqual(image.getpixel((0, 0)), (255, 64, 192))

    def test_multi_material_evidence_tracks_face_groups_in_every_actual_instance(self):
        from PIL import Image
        first = SimpleNamespace(name='same', baseColorFactor=ListValues([0, 0, 0, 255]),
            roughnessFactor=0, metallicFactor=0, emissiveFactor=ListValues([0, 0, 0]),
            alphaMode='MASK', alphaCutoff=0, baseColorTexture=Image.new('RGB', (2, 3), 'black'))
        second = SimpleNamespace(name='same', baseColorFactor=ListValues([20, 50, 230, 255]),
            roughnessFactor=.8, metallicFactor=.5, emissiveFactor=ListValues([0, .1, 1]),
            alphaMode='OPAQUE', alphaCutoff=.5, baseColorTexture=Image.new('RGB', (4, 2), 'blue'))
        mesh = SimpleNamespace(vertices=[0, 1, 2, 3], faces=[[0, 1, 2], [1, 2, 3]],
            visual=SimpleNamespace(kind='texture', uv=[[0, 0], [1, 0], [0, 1], [1, 1]],
                                   material=SimpleNamespace(materials=[first, second]), face_materials=[1, 0]))
        matrices = {'instance_a': ListValues([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]),
                    'instance_b': ListValues([[1, 0, 0, 3], [0, 1, 0, 1], [0, 0, 1, 2], [0, 0, 0, 1]])}
        class Graph:
            nodes_geometry = ['instance_b', 'instance_a']
            def __getitem__(self, name):
                return matrices[name], 'mesh'
        stats = _visual_stats(SimpleNamespace(geometry={'mesh': mesh}, graph=Graph()))
        self.assertEqual(stats['materialCount'], 2)
        self.assertEqual(stats['texturedMeshes'], 1)
        self.assertTrue(stats['uvVerified'])
        self.assertEqual(stats['imageCount'], 2)
        self.assertEqual(stats['materials'][0]['roughnessFactor'], 0)
        self.assertEqual(stats['materials'][0]['emissiveFactor'], [0, 0, 0])
        self.assertEqual(stats['materials'][0]['alphaCutoff'], 0)
        self.assertEqual([(item['node'], item['geometry'], item['materialIndex'], item['faceIndices'])
                          for item in stats['bindings']],
                         [('instance_a', 'mesh', 0, [1]), ('instance_a', 'mesh', 1, [0]),
                          ('instance_b', 'mesh', 0, [1]), ('instance_b', 'mesh', 1, [0])])
        self.assertEqual(stats['bindings'][2]['transform'], matrices['instance_b'])

    def test_nested_multi_material_texture_cannot_bypass_missing_uv_validation(self):
        from PIL import Image
        mesh = SimpleNamespace(vertices=[0, 1, 2], faces=[[0, 1, 2]],
            visual=SimpleNamespace(kind='texture', uv=None, face_materials=[0],
                                   material=SimpleNamespace(materials=[SimpleNamespace(
                                       normalTexture=Image.new('RGB', (3, 2), (128, 128, 255)))])))
        with self.assertRaisesRegex(ValueError, 'missing UV'):
            _visual_stats(SimpleNamespace(geometry={'mesh': mesh}))

    def test_colour_only_pbr_material_does_not_require_texture_uv(self):
        material = SimpleNamespace(name='blue', baseColorFactor=ListValues([20, 50, 230, 255]),
                                   metallicFactor=.1, roughnessFactor=.8, alphaMode='OPAQUE', doubleSided=False)
        mesh = SimpleNamespace(vertices=[0, 1, 2], faces=[0],
                               visual=SimpleNamespace(kind='texture', uv=None, material=material))
        stats = _visual_stats(SimpleNamespace(geometry={'mesh': mesh}))
        self.assertEqual(stats['materialCount'], 1)
        self.assertEqual(stats['materials'][0]['baseColorFactor'], [20, 50, 230, 255])
        self.assertEqual(stats['texturedMeshes'], 0)
        self.assertFalse(stats['uvVerified'])
        self.assertEqual(stats['imageCount'], 0)

    def test_actual_texture_map_still_requires_uv_for_every_role(self):
        from PIL import Image
        for role in ('baseColorTexture', 'normalTexture', 'metallicRoughnessTexture', 'emissiveTexture', 'occlusionTexture', 'image'):
            with self.subTest(role=role):
                material = SimpleNamespace(**{role: Image.new('RGB', (4, 4), (20, 50, 230))})
                mesh = SimpleNamespace(vertices=[0, 1, 2], faces=[0],
                                       visual=SimpleNamespace(kind='texture', uv=None, material=material))
                with self.assertRaisesRegex(ValueError, 'missing UV'):
                    _visual_stats(SimpleNamespace(geometry={'mesh': mesh}))

    def test_concurrent_preparation_reuses_one_immutable_complete_asset(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, source = source_fixture(root)
            calls = []
            def load(*args, **kwargs):
                calls.append(1)
                time.sleep(.05)
                return stub_scene()
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=load)}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), ThreadPoolExecutor(2) as executor:
                reports = list(executor.map(lambda _: prepare(legacy, root / 'data', 'chair'), range(2)))
            self.assertEqual(len(calls), 1)
            self.assertEqual(reports[0], reports[1])
            report = reports[0]
            self.assertEqual(Path(report['path']).read_bytes(), glb())
            self.assertEqual(report['visualDigest'], hashlib.sha256(glb()).hexdigest())
            self.assertIn('report.json', report['preparedFiles'])
            self.assertTrue(any(name.startswith('provenance/input/') for name in report['preparedFiles']))

    def test_external_texture_content_changes_preparation_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, source = source_fixture(root, 'chair.obj', b'mtllib chair.mtl\nv 0 0 0\n')
            (legacy / 'chair.mtl').write_text('newmtl chair\nmap_Kd color.png\n')
            texture = legacy / 'color.png'
            texture.write_bytes(b'first texture')
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                before = prepare(legacy, root / 'data', 'chair')
                texture.write_bytes(b'second texture')
                after = prepare(legacy, root / 'data', 'chair')
            self.assertNotEqual(before['digest'], after['digest'])
            self.assertTrue(Path(before['reportPath']).exists())
            self.assertEqual(len(after['inputs']['dependencies']), 3)

    def test_lookup_evidence_does_not_change_local_preparation_inputs_or_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, _ = source_fixture(root)
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), \
                    patch('runtime.assets._versions', return_value={'algorithm': 'old'}):
                previous = prepare(legacy, root / 'data', 'chair')
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), \
                    patch('runtime.assets._versions', return_value={'algorithm': 'current'}):
                current = prepare(legacy, root / 'data', 'chair')
                self.assertTrue(assets(legacy, 'chair', root=root / 'data')['items'][0]['rigidReady'])
                with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: self.fail('cached model was loaded again'))}):
                    repeated = prepare(legacy, root / 'data', 'chair')
            self.assertEqual(current, repeated)
            self.assertNotEqual(current['digest'], previous['digest'])
            self.assertNotIn('preparationReports', current['inputs']['sourceMetadata'])
            self.assertEqual(json.loads(Path(previous['reportPath']).read_text()), previous)

    def test_prepared_local_and_generated_cross_category_keywords_remain_retrievable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, source = source_fixture(root)
            catalog = legacy / 'assets/ready_asset_manifest.json'
            document = json.loads(catalog.read_text())
            document['records'][0].update(asset_id='attachment', name='tool attachment used with chair',
                                         description='tool attachment used with chair', category='tool')
            catalog.write_text(json.dumps(document))
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                native = prepare(legacy, root / 'data', 'attachment')
                generated = prepare_generated(root / 'data', source, {'asset.py': '# generated attachment'},
                                              'asset.py', [], name='tool attachment used with chair')
            for report in (native, generated):
                with self.subTest(asset_id=report['assetId']):
                    by_id = assets(legacy, root=root / 'data', asset_id=report['assetId'])['items'][0]
                    self.assertTrue(by_id['currentPreparation'])
                    self.assertEqual(by_id['category'], 'tool')
                    candidates = assets(legacy, 'chair', root=root / 'data')['items']
                    candidate = next((item for item in candidates if item['id'] == report['assetId']), None)
                    self.assertIsNotNone(candidate)
                    self.assertTrue(candidate['currentPreparation'])
                    explicit = assets(legacy, 'chair', root=root / 'data', category='chair')['items']
                    self.assertNotIn(report['assetId'], [item['id'] for item in explicit])

    def test_prepared_material_reuses_original_maps_after_lookup_projection(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / 'legacy'
            source = legacy / 'data/materials'
            source.mkdir(parents=True)
            maps = {}
            for name, color in [('albedo', (90, 60, 30)), ('roughness', (64, 64, 64))]:
                path = source / (name + '.png')
                Image.new('RGB', (3, 2), color).save(path)
                maps[name] = str(path)
            index = source / 'sceneagent-material-index.json'
            index.write_text(json.dumps([{'material_id': 'ambientcg:WoodFixture', 'name': 'Continuous wood',
                'category': 'wood', 'tags': ['continuous', 'wood'], 'textures': maps, 'license': 'CC0'}]))
            first = prepare(legacy, root / 'data', 'ambientcg:WoodFixture')
            item = assets(legacy, 'continuous wood', root=root / 'data', kind='material')['items'][0]
            self.assertTrue(item['rigidReady'])
            self.assertEqual(item['reportPath'], first['reportPath'])
            furniture_material = assets(legacy, 'wood chair', root=root / 'data', kind='material')['items'][0]
            self.assertTrue(furniture_material['currentPreparation'])
            repeated = prepare(legacy, root / 'data', 'ambientcg:WoodFixture')
            self.assertEqual(first, repeated)
            self.assertEqual(repeated['inputs']['maps'], maps)

    def test_cache_requires_positive_complete_manifest_and_core_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output.glb'
            output.write_bytes(glb())
            files = {'asset.py': '# generation'}
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                report = prepare_generated(root / 'data', output, files, 'asset.py', [])
            directory = Path(report['reportPath']).parent
            self.assertEqual(_cache_valid(directory), report)
            for key in ('frozenDigests', 'dependencyFiles', 'preparedFiles'):
                with self.subTest(missing=key):
                    changed = {field: value for field, value in report.items() if field != key}
                    Path(report['reportPath']).write_text(json.dumps(changed))
                    self.assertIsNone(_cache_valid(directory))
            for filename in ('visual.glb', 'provenance/source/asset.py'):
                with self.subTest(unlisted=filename):
                    changed = {**report, 'frozenDigests': {name: value for name, value in report['frozenDigests'].items() if name != filename},
                               'dependencyFiles': [name for name in report['dependencyFiles'] if name != filename],
                               'preparedFiles': [name for name in report['preparedFiles'] if name != filename]}
                    Path(report['reportPath']).write_text(json.dumps(changed))
                    self.assertIsNone(_cache_valid(directory))
            Path(report['reportPath']).write_text(json.dumps(report))
            (directory / 'unlisted-input.bin').write_bytes(b'unlisted dependency')
            self.assertIsNone(_cache_valid(directory))

    def test_declared_rendered_thumbnail_requires_a_hashed_file_inside_the_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output.glb'
            output.write_bytes(glb())
            def thumbnail(directory, up_axis):
                (directory / 'thumbnail.png').write_bytes(b'rendered image fixture')
                return {'status': 'rendered', 'path': 'thumbnail.png'}
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', side_effect=thumbnail):
                report = prepare_generated(root / 'data', output, {'asset.py': '# generation'}, 'asset.py', [], name='chair')
            directory = Path(report['reportPath']).parent
            self.assertEqual(_cache_valid(directory), report)
            self.assertTrue(assets(root / 'legacy', 'chair', root=root / 'data')['items'][0]['visualReady'])
            changed = {**report, 'frozenDigests': {name: digest for name, digest in report['frozenDigests'].items() if name != 'thumbnail.png'},
                       'dependencyFiles': [name for name in report['dependencyFiles'] if name != 'thumbnail.png'],
                       'preparedFiles': [name for name in report['preparedFiles'] if name != 'thumbnail.png']}
            (directory / 'thumbnail.png').unlink()
            Path(report['reportPath']).write_text(json.dumps(changed))
            self.assertIsNone(_cache_valid(directory))
            item = assets(root / 'legacy', 'chair', root=root / 'data')['items'][0]
            self.assertFalse(item['visualReady'])

    def test_report_path_must_bind_the_actual_prepared_report_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output.glb'
            output.write_bytes(glb())
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                report = prepare_generated(root / 'data', output, {'asset.py': '# generation'}, 'asset.py', [], name='chair')
            actual = Path(report['reportPath'])
            actual.write_text(json.dumps({**report, 'reportPath': '/unrelated/missing/report.json'}))
            self.assertIsNone(_cache_valid(actual.parent))
            item = assets(root / 'legacy', 'chair', root=root / 'data', asset_id=report['assetId'])['items'][0]
            self.assertFalse(item['currentPreparation'])
            self.assertFalse(item['rigidReady'])
            self.assertEqual(item['reportPath'], str(actual))

    def test_mesh_loader_reads_frozen_bytes_when_original_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = glb()
            legacy, source = source_fixture(root, content=original)
            def load(path, **kwargs):
                self.assertIn('provenance/input', str(path))
                source.write_bytes(glb({'asset': {'version': '2.0'}, 'meshes': [{'changed': True}]}))
                self.assertEqual(Path(path).read_bytes(), original)
                return stub_scene()
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=load)}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                report = prepare(legacy, root / 'data', 'chair')
            frozen = [name for name in report['preparedFiles'] if name.endswith('chair.glb')][0]
            self.assertEqual((Path(report['reportPath']).parent / frozen).read_bytes(), original)

    def test_missing_or_escaped_external_image_is_rejected_before_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, source = source_fixture(root, 'chair.obj', b'mtllib chair.mtl\n')
            material = legacy / 'chair.mtl'
            material.write_text('map_Kd missing.png\n')
            with self.assertRaisesRegex(ValueError, 'missing'):
                dependency_paths(source, allowed_root=legacy)
            (root / 'outside.png').write_bytes(b'outside')
            material.write_text('map_Kd ../outside.png\n')
            with self.assertRaisesRegex(ValueError, 'escapes'):
                dependency_paths(source, allowed_root=legacy)

    def test_generated_source_is_frozen_and_same_inputs_hit_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output.glb'
            output.write_bytes(glb())
            files = {'scene.py': '# scene', 'custom.py': 'def build_asset(materials): pass'}
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                report = prepare_generated(root / 'data', output, files, 'custom.py', [], name='custom asset')
            self.assertEqual(generated_cached_report(root / 'data', files, 'custom.py', []), report)
            self.assertEqual((Path(report['reportPath']).parent / 'provenance/source/custom.py').read_text(), files['custom.py'])
            self.assertIsNone(generated_cached_report(root / 'data', {**files, 'custom.py': '# changed'}, 'custom.py', []))

    def test_cache_does_not_accept_changed_frozen_texture_or_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output.glb'
            output.write_bytes(glb())
            files = {'asset.py': '# generation'}
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                report = prepare_generated(root / 'data', output, files, 'asset.py', [])
            (Path(report['reportPath']).parent / 'provenance/source/asset.py').write_text('# tampered')
            self.assertIsNone(generated_cached_report(root / 'data', files, 'asset.py', []))

    def test_online_glb_header_is_checked(self):
        with self.assertRaisesRegex(ValueError, 'GLB'):
            _glb_document(b'<html>not a model</html>')

    def test_online_external_uri_is_rejected_before_a_mesh_loader_can_fetch_it(self):
        content = glb({'asset': {'version': '2.0'}, 'meshes': [{}], 'images': [{'uri': 'http://127.0.0.1/secret.png'}]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, receipt, _ = self.staged_import(root, content)
            with self.assertRaisesRegex(ValueError, 'external dependencies'):
                prepare_online(root / 'legacy', root, receipt['url'], import_ref=reference)

    def test_staged_online_oversize_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, receipt, source = self.staged_import(root)
            with (source / 'source.glb').open('ab') as file:
                file.truncate(25 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(ValueError, 'size limit'):
                prepare_online(root / 'legacy', root, receipt['url'], import_ref=reference)

    def test_same_staged_source_refreshes_metadata_and_preparation_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, retrieval, _ = self.staged_import(root / 'data')
            url = retrieval['url']
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), \
                    patch('runtime.assets._versions', return_value={'algorithm': 'old'}):
                first = prepare_online(root / 'legacy', root / 'data', url, import_ref=reference,
                    name='old chair', source_page='https://example.com/old', license='CC0')
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), \
                    patch('runtime.assets._versions', return_value={'algorithm': 'new'}):
                second = prepare_online(root / 'legacy', root / 'data', url, import_ref=reference,
                    name='new chair', source_page='https://example.com/new', license='CC-BY-4.0')
            self.assertNotEqual(first['digest'], second['digest'])
            self.assertEqual(second['name'], 'new chair')
            self.assertEqual(second['inputs']['versions'], {'algorithm': 'new'})
            self.assertEqual(second['source']['sourcePage'], 'https://example.com/new')
            self.assertEqual(second['source']['licenseEvidence'], {'status': 'caller_declaration_unverified', 'value': 'CC-BY-4.0'})
            self.assertEqual(second['source']['retrieval']['retrievedAt'], retrieval['retrievedAt'])
            from runtime.assets import assets
            indexed = assets(root / 'legacy', 'new chair', root=root / 'data')['items']
            self.assertEqual(indexed[0]['id'], second['assetId'])
            self.assertEqual(indexed[0]['name'], 'new chair')
            self.assertEqual(indexed[0]['licenseEvidence'], second['source']['licenseEvidence'])

    def test_online_import_skips_legacy_reports_with_string_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_report = root / 'data/assets/prepared/old/report.json'
            legacy_report.parent.mkdir(parents=True)
            legacy_report.write_text(json.dumps({'assetId': 'artvip:knife', 'source': 'artvip', 'kind': 'static_mesh'}))
            reference, retrieval, _ = self.staged_import(root / 'data')
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                report = prepare_online(root / 'legacy', root / 'data', retrieval['url'], import_ref=reference, name='chair')
                repeated = prepare_online(root / 'legacy', root / 'data', retrieval['url'], import_ref=reference, name='chair')
            self.assertEqual(report, repeated)

    def staged_import(self, root, content=None, url='https://example.com/chair.glb'):
        content = content if content is not None else glb()
        digest = hashlib.sha256(content).hexdigest()
        receipt = {'url': url, 'finalUrl': url, 'sha256': digest, 'bytes': len(content), 'httpStatus': 200,
                   'retrievedAt': '2026-01-01T00:00:00Z', 'transport': 'mac_system_curl'}
        encoded = json.dumps(receipt).encode()
        receipt_digest = hashlib.sha256(encoded).hexdigest()
        directory = root / 'assets/imports' / digest
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'source.glb').write_bytes(content)
        (directory / ('retrieval-' + receipt_digest + '.json')).write_bytes(encoded)
        return {'digest': digest, 'retrievalDigest': receipt_digest}, receipt, directory

    def test_old_online_id_reprepares_its_own_frozen_source_offline(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broken = root / 'assets/prepared/unrelated/report.json'
            broken.parent.mkdir(parents=True)
            broken.write_text('{broken unrelated report')
            reference, receipt, _ = self.staged_import(root)
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                with patch('runtime.assets._versions', return_value={'algorithm': 'old'}):
                    previous = prepare_online(root / 'legacy', root, receipt['url'], import_ref=reference,
                        name='original chair', source_page='https://example.com/catalog', license='CC0', project_id='original')
                shutil.rmtree(root / 'assets/imports')
                with patch('runtime.assets._versions', return_value={'algorithm': 'current'}), patch(
                        'socket.create_connection', side_effect=AssertionError('offline preparation opened network')):
                    current = prepare(root / 'legacy', root, previous['assetId'])
            self.assertNotEqual(current['digest'], previous['digest'])
            self.assertEqual(current['assetId'], previous['assetId'])
            self.assertEqual(current['source'], previous['source'])
            self.assertEqual(current['name'], previous['name'])
            self.assertEqual(current['license'], previous['license'])
            self.assertEqual(current['inputs']['versions'], {'algorithm': 'current'})
            frozen = Path(current['reportPath']).parent
            raw = [name for name, digest in current['frozenDigests'].items()
                   if name.startswith('provenance/input/') and digest == receipt['sha256']]
            self.assertEqual(len(raw), 1)
            self.assertEqual(hashlib.sha256((frozen / raw[0]).read_bytes()).hexdigest(), receipt['sha256'])
            self.assertTrue(any('retrieval-' in name for name in current['frozenDigests']))
            self.assertEqual(json.loads(Path(previous['reportPath']).read_text()), previous)

    def test_old_online_source_without_import_ref_still_reprepares_offline(self):
        import shutil
        from runtime.assets import _dependencies, _publish_model
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, receipt, original = self.staged_import(root)
            path = original / 'source.glb'
            source = {'kind': 'online_import', 'projectId': 'historical', 'retrieval': receipt,
                      'sourcePage': 'https://example.com/catalog',
                      'licenseEvidence': {'status': 'caller_declaration_unverified', 'value': 'CC0'}}
            item = {'id': 'imported:' + receipt['sha256'], 'name': 'historical chair', 'source': 'online_import',
                    'path': str(path), 'upAxis': 'Y', 'units': 'unknown', 'license': 'CC0'}
            inputs = {'versions': {'algorithm': 'old'}, 'dependencies': _dependencies([path]),
                      'upAxis': 'Y', 'units': 'unknown', 'name': item['name'], 'source': source}
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                previous = _publish_model(root, item, [path], inputs, source=source)
                shutil.rmtree(root / 'assets/imports')
                with patch('runtime.assets._versions', return_value={'algorithm': 'current'}), patch(
                        'socket.create_connection', side_effect=AssertionError('legacy source opened network')):
                    current = prepare(root / 'legacy', root, previous['assetId'])
            self.assertEqual(current['assetId'], previous['assetId'])
            self.assertEqual(current['source'], source)
            self.assertNotIn('importRef', current['source'])
            self.assertNotEqual(current['digest'], previous['digest'])

    def test_old_online_repreparation_rejects_missing_or_changed_frozen_inputs(self):
        for label in ('source', 'receipt'):
            for missing in (False, True):
                with self.subTest(label=label, missing=missing), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    reference, receipt, _ = self.staged_import(root)
                    with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                            patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), \
                            patch('runtime.assets._versions', return_value={'algorithm': 'old'}):
                        previous = prepare_online(root / 'legacy', root, receipt['url'], import_ref=reference, name='chair')
                    filename = next(name for name in previous['frozenDigests'] if name.startswith('provenance/input/')
                                    and (name.endswith('.glb') if label == 'source' else 'retrieval-' in name))
                    path = Path(previous['reportPath']).parent / filename
                    path.unlink() if missing else path.write_bytes(b'changed frozen input')
                    with patch('runtime.assets._versions', return_value={'algorithm': 'current'}), patch(
                            'socket.create_connection', side_effect=AssertionError('bad source opened network')):
                        with self.assertRaisesRegex(ValueError, 'changed|missing'):
                            prepare(root / 'legacy', root, previous['assetId'])

    def test_old_online_repreparation_binds_receipt_to_original_import_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, receipt, _ = self.staged_import(root)
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), \
                    patch('runtime.assets._versions', return_value={'algorithm': 'old'}):
                previous = prepare_online(root / 'legacy', root, receipt['url'], import_ref=reference, name='chair')
            prepared = Path(previous['reportPath']).parent
            name = next(name for name in previous['frozenDigests'] if name.startswith('provenance/input/') and 'retrieval-' in name)
            changed = json.dumps({**receipt, 'url': 'https://example.com/another-origin.glb'}).encode()
            (prepared / name).write_bytes(changed)
            previous['frozenDigests'][name] = hashlib.sha256(changed).hexdigest()
            (prepared / 'report.json').write_text(json.dumps(previous))
            self.assertIsNotNone(_cache_valid(prepared))
            with patch('runtime.assets._versions', return_value={'algorithm': 'current'}):
                with self.assertRaisesRegex(ValueError, 'receipt'):
                    prepare(root / 'legacy', root, previous['assetId'])

    def test_new_online_url_requires_mac_staged_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('socket.getaddrinfo', side_effect=AssertionError('un-staged URL opened network')) as resolve:
                with self.assertRaisesRegex(ValueError, 'importRef|staged'):
                    prepare_online(Path(directory) / 'legacy', directory, 'https://example.com/chair.glb')
                resolve.assert_not_called()

    def test_staged_online_asset_is_verified_and_prepared_without_gpu_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, receipt, _ = self.staged_import(root)
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), \
                    patch('socket.create_connection', side_effect=AssertionError('GPU network called')):
                report = prepare_online(root / 'legacy', root, receipt['url'], import_ref=reference, name='chair')
            self.assertEqual(report['source']['retrieval'], receipt)
            self.assertTrue(any('retrieval-' in name for name in report['preparedFiles']))

    def test_staged_import_rejects_bad_reference_hash_url_and_external_uri(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, receipt, source = self.staged_import(root)
            with self.assertRaisesRegex(ValueError, 'digest'):
                prepare_online(root / 'legacy', root, receipt['url'], import_ref={**reference, 'digest': '../outside'})
            with self.assertRaisesRegex(ValueError, 'URL'):
                prepare_online(root / 'legacy', root, 'https://example.com/other.glb', import_ref=reference)
            (source / 'source.glb').write_bytes(b'changed source')
            with self.assertRaisesRegex(ValueError, 'digest'):
                prepare_online(root / 'legacy', root, receipt['url'], import_ref=reference)
            content = glb({'asset': {'version': '2.0'}, 'meshes': [{}], 'images': [{'uri': 'https://example.com/other.png'}]})
            reference, receipt, _ = self.staged_import(root, content)
            with self.assertRaisesRegex(ValueError, 'external dependencies'):
                prepare_online(root / 'legacy', root, receipt['url'], import_ref=reference)

    def test_same_staged_bytes_keep_distinct_origin_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, receipt_a, _ = self.staged_import(root, url='https://example.com/a.glb')
            second, receipt_b, source = self.staged_import(root, url='https://example.com/b.glb')
            self.assertEqual(first['digest'], second['digest'])
            self.assertNotEqual(first['retrievalDigest'], second['retrievalDigest'])
            with patch.dict('sys.modules', {'trimesh': SimpleNamespace(load=lambda *a, **k: stub_scene())}), \
                    patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}), \
                    patch('socket.create_connection', side_effect=AssertionError('GPU network called')):
                a = prepare_online(root / 'legacy', root, receipt_a['url'], import_ref=first, name='chair')
                b = prepare_online(root / 'legacy', root, receipt_b['url'], import_ref=second, name='chair')
            self.assertNotEqual(a['digest'], b['digest'])
            self.assertEqual(a['source']['retrieval']['url'], receipt_a['url'])
            self.assertEqual(b['source']['retrieval']['url'], receipt_b['url'])


@unittest.skipUnless(importlib.util.find_spec('trimesh'), 'real mesh checks run on the configured 4090 runtime')
class RealAssetPreparationTests(unittest.TestCase):
    def test_native_multi_material_face_groups_and_repeated_scene_instances_are_measured(self):
        import numpy as np
        import trimesh
        from PIL import Image
        mesh = trimesh.creation.box()
        materials = [trimesh.visual.material.PBRMaterial(name='duplicate-name',
            baseColorTexture=Image.new('RGB', (3, 2), color), roughnessFactor=roughness,
            emissiveFactor=[0, 0, 0], alphaMode='MASK', alphaCutoff=0)
            for color, roughness in [('black', 0), ('white', .5)]]
        indices = np.arange(len(mesh.faces)) % 2
        mesh.visual = trimesh.visual.TextureVisuals(uv=np.tile([.2, .8], (len(mesh.vertices), 1)),
            material=trimesh.visual.material.MultiMaterial(materials), face_materials=indices)
        scene = trimesh.Scene()
        scene.add_geometry(mesh, geom_name='grouped', node_name='first')
        transform = trimesh.transformations.translation_matrix([3, 1, 2])
        scene.graph.update(frame_to='second', matrix=transform, geometry='grouped')
        evidence = _visual_stats(scene)
        self.assertEqual(evidence['materialCount'], 2)
        self.assertEqual(evidence['imageCount'], 2)
        self.assertEqual(len(evidence['bindings']), 4)
        for item in evidence['bindings']:
            np.testing.assert_allclose(item['transform'], scene.graph[item['node']][0])
            self.assertEqual(item['faceIndices'], np.flatnonzero(indices == item['materialIndex']).tolist())
        self.assertEqual(evidence['materials'][0]['roughnessFactor'], 0)
        self.assertEqual(evidence['materials'][0]['alphaCutoff'], 0)

    def test_multiple_materials_uv_and_child_transforms_survive_preparation(self):
        import numpy as np
        import trimesh
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene = trimesh.Scene()
            for name, color, translation in [('red', (240, 20, 30), [0, 0, 0]), ('green', (10, 210, 40), [3, 1, 2])]:
                mesh = trimesh.creation.box()
                material = trimesh.visual.material.PBRMaterial(name=name, baseColorTexture=Image.new('RGB', (8, 8), color),
                                                              metallicFactor=.25, roughnessFactor=.6)
                mesh.visual = trimesh.visual.TextureVisuals(uv=np.tile([.2, .8], (len(mesh.vertices), 1)), material=material)
                transform = np.eye(4)
                transform[:3, 3] = translation
                scene.add_geometry(mesh, node_name=name, geom_name=name, transform=transform)
            legacy, source = source_fixture(root, content=scene.export(file_type='glb'))
            with patch('runtime.assets._thumbnail', return_value={'status': 'unavailable'}):
                report = prepare(legacy, root / 'data', 'chair')
            loaded = trimesh.load(report['path'], force='scene')
            self.assertEqual(len(loaded.geometry), 2)
            self.assertEqual(report['textureEvidence']['materialCount'], 2)
            np.testing.assert_allclose(loaded.bounds, scene.bounds)
            self.assertEqual({mesh.visual.material.name for mesh in loaded.geometry.values()}, {'red', 'green'})
            for mesh in loaded.geometry.values():
                self.assertEqual(mesh.visual.kind, 'texture')
                self.assertAlmostEqual(mesh.visual.material.metallicFactor, .25)
                self.assertAlmostEqual(mesh.visual.material.roughnessFactor, .6)
                np.testing.assert_allclose(mesh.visual.uv, np.tile([.2, .8], (len(mesh.vertices), 1)), atol=1e-7)


if __name__ == '__main__':
    unittest.main()
