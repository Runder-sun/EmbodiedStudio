import json
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from runtime.assets import assets, _json_hash, _register, _source_rows, _versions


class AssetIndexTests(unittest.TestCase):
    def collection_fixture(self, root, *, count=1, source_root='current_assets'):
        legacy, data = root / 'legacy', root / 'data'
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / 'fixture.obj').write_text('v 0 0 0\n')
        catalog = data / 'assets/collections/catalog.json'
        catalog.parent.mkdir(parents=True, exist_ok=True)
        bases = {'current_assets': data / 'assets', 'legacy_assets': legacy / 'assets',
                 'legacy_data': legacy / 'data'}
        rows = []
        for index in range(count):
            package = 'collections/fixture/v1/chair-' + str(index)
            directory = bases[source_root] / package
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'model.usda').write_text('#usda 1.0\ndef Xform "Chair" {}\n')
            rows.append({'assetId': 'collection:fixture:v1:chair-' + str(index), 'collection': 'fixture-v1',
                'name': 'office chair', 'description': 'A black office chair.', 'category': 'chair',
                'aliases': ['task seating'], 'sourceRoot': source_root, 'packageRoot': package,
                'entry': 'model.usda', 'dependencyFiles': [], 'format': 'native_files', 'kind': 'model',
                'upAxis': 'Z', 'units': 'meters', 'qualityEvidence': {'status': 'not_individually_reviewed'},
                'importRequirements': {'materials': 'native_activation_unverified'}, 'simulationReady': True})
        document = {'schema': 'sceneagent-asset-collections-v1', 'collections': [{'id': 'fixture-v1',
            'sourcePage': 'https://example.com/fixture', 'revision': 'v1', 'libraryTier': 'preferred',
            'licenseEvidence': {'status': 'source_declaration', 'value': 'CC0'},
            'qualityEvidence': {'status': 'official_samples_reviewed', 'scope': 'source_samples'}}], 'assets': rows}
        catalog.write_text(json.dumps(document))
        return legacy, data, catalog, document

    def test_current_root_collection_is_searchable_and_rebuildable_without_readiness_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, _ = self.collection_fixture(Path(directory))
            first = assets(legacy, 'task seating', root=data, libraryTier='preferred')['items'][0]
            self.assertEqual(first['id'], 'collection:fixture:v1:chair-0')
            self.assertEqual(first['libraryTier'], 'preferred')
            self.assertEqual(first['qualityEvidence']['status'], 'not_individually_reviewed')
            self.assertFalse(first['simulationReady'])
            self.assertFalse(first['filesFrozen'])
            self.assertNotIn('dependencyFiles', first)
            (data / 'library/assets-v3.sqlite').unlink()
            second = assets(legacy, 'task seating', root=data, libraryTier='preferred')['items'][0]
            self.assertEqual(first, second)

    def test_existing_four_column_cache_is_rebuilt_without_losing_prepared_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, report = self.prepared_fixture(Path(directory), register=False)
            cache = data / 'library/assets-v3.sqlite'
            cache.parent.mkdir(parents=True)
            with sqlite3.connect(cache) as connection:
                connection.execute('CREATE TABLE assets (id TEXT PRIMARY KEY, kind TEXT, category TEXT, payload TEXT)')
            item = assets(legacy, 'office chair', root=data, libraryTier='broad')['items'][0]
            self.assertEqual(item['reportPath'], report['reportPath'])
            self.assertTrue(item['currentPreparation'])
            self.assertEqual(len(list(cache.parent.glob('*.sqlite'))), 1)

    def test_library_tier_is_filtered_before_the_sql_candidate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, _ = self.collection_fixture(Path(directory))
            catalog = legacy / 'assets/ready_asset_manifest.json'
            catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps({'records': [{'asset_id': 'broad:' + str(index),
                'name': 'office chair', 'description': 'office chair', 'category': 'chair',
                'visual_mesh_path': 'fixture.obj'} for index in range(1300)]}))
            preferred = assets(legacy, 'office chair', root=data, libraryTier='preferred')
            self.assertEqual(preferred['matched'], 1)
            self.assertEqual(preferred['items'][0]['id'], 'collection:fixture:v1:chair-0')
            self.assertTrue(all(item['libraryTier'] == 'broad' for item in
                assets(legacy, 'office chair', root=data, libraryTier='broad')['items']))

    def test_untranslated_chinese_query_does_not_become_an_unfiltered_browse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self.fixture(root)
            result = assets(legacy, '量子收纳盒', root=root / 'data')
            self.assertEqual(result['matched'], 0)
            self.assertEqual(result['items'], [])

    def test_appliance_names_synonyms_and_hierarchical_categories_are_searchable(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory), count=3)
            for row, name, category in zip(document['assets'],
                    ('Refrigerator042', 'WashingMachine002', 'Drawer111'),
                    ('fixtures/refrigerators', 'fixtures/washing_machines', 'furniture/storage/drawers')):
                row.update(name=name, description='Original source model.', category=category, aliases=[])
            catalog.write_text(json.dumps(document))
            for query, index in (('冰箱', 0), ('fridge', 0), ('fridges', 0), ('refrigerator', 0),
                                 ('洗衣机', 1), ('抽屉', 2)):
                with self.subTest(query=query):
                    result = assets(legacy, query, root=data, libraryTier='preferred')
                    self.assertTrue(result['items'])
                    self.assertEqual(result['items'][0]['id'], document['assets'][index]['assetId'])
            result = assets(legacy, root=data, category='objects/kitchen/fridges')
            self.assertEqual([row['id'] for row in result['items']], [document['assets'][0]['assetId']])

    def test_household_synonyms_recall_original_source_names_and_explain_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory), count=6)
            names = ('nightstand', 'BedsideTable007', 'bedside cabinet', 'table bedside b01', 'sofa', 'couch')
            for row, name in zip(document['assets'], names):
                row.update(name=name, description='Original source model.', category='', aliases=[])
            catalog.write_text(json.dumps(document))
            original = catalog.read_bytes()
            groups = [(('nightstand', 'nightstands', 'bedside table', 'bedside cabinets', '床头柜'), range(4), 'cabinet'),
                      (('sofa', 'sofas', 'couch', 'couches'), range(4, 6), 'sofa')]
            for queries, indices, category in groups:
                expected = {document['assets'][index]['assetId'] for index in indices}
                for query in queries:
                    with self.subTest(query=query):
                        result = assets(legacy, query, root=data, libraryTier='preferred', category=category)
                        self.assertEqual({item['id'] for item in result['items']}, expected)
                        for item in result['items']:
                            index = [row['assetId'] for row in document['assets']].index(item['id'])
                            self.assertEqual(item['name'], names[index])
                            self.assertEqual(item['description'], 'Original source model.')
                            self.assertEqual(item['sourceCategory'], '')
                            self.assertEqual(item['semanticMatch']['matchedWords'], item['semanticMatch']['query'].split())
            self.assertEqual(catalog.read_bytes(), original)

    def test_household_synonyms_keep_word_boundaries_and_explicit_object_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory), count=8)
            specs = [('bedside table lamp', ''), ('cabinet with table lamp', 'cabinet'),
                     ('dining table', 'table'), ('nightstander fitting', 'tool'), ('couching fixture', 'tool'),
                     ('cabinet door', 'door'), ('bedside cabinet', 'cabinet'), ('bedside table', 'table')]
            for row, (name, category) in zip(document['assets'], specs):
                row.update(name=name, description='Original source model.', category=category, aliases=[])
            catalog.write_text(json.dumps(document))
            found = assets(legacy, 'nightstand', root=data, category='cabinet')['items']
            self.assertEqual([item['id'] for item in found], [document['assets'][6]['assetId']])
            tables = assets(legacy, 'nightstand', root=data, category='table')['items']
            self.assertEqual([item['id'] for item in tables], [document['assets'][7]['assetId']])
            self.assertEqual(tables[0]['category'], 'table')
            self.assertEqual(tables[0]['sourceCategory'], 'table')
            lamps = assets(legacy, 'bedside table', root=data, category='lamp')['items']
            self.assertEqual([item['id'] for item in lamps], [document['assets'][0]['assetId']])
            self.assertEqual(lamps[0]['category'], 'lamp')
            cabinet = assets(legacy, 'cabinet', root=data, category='cabinet')['items']
            self.assertIn(document['assets'][1]['assetId'], [item['id'] for item in cabinet])
            self.assertEqual(assets(legacy, 'sofa', root=data)['items'], [])
            plain = assets(legacy, 'nightstand', root=data)['items']
            self.assertNotIn(document['assets'][3]['assetId'], [item['id'] for item in plain])
            doors = assets(legacy, 'cabinet door', root=data, category='door')['items']
            self.assertEqual([item['id'] for item in doors], [document['assets'][5]['assetId']])

    def test_old_search_normalization_index_is_rebuilt_without_changing_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory))
            document['assets'][0].update(name='bedside table', description='Original source model.', category='', aliases=[])
            catalog.write_text(json.dumps(document))
            original = catalog.read_bytes()
            assets(legacy, root=data)
            old_signature = _json_hash({'indexSchema': 10, 'legacy': str(legacy.resolve()),
                'files': [(str(catalog), hashlib.sha256(original).hexdigest())]})
            with sqlite3.connect(data / 'library/assets-v3.sqlite') as connection:
                connection.execute('UPDATE search SET text=?', ('bedside table original source model table',))
                connection.execute("UPDATE metadata SET value=? WHERE key='sources'", (old_signature,))
            result = assets(legacy, 'nightstand', root=data)
            self.assertEqual([item['id'] for item in result['items']], [document['assets'][0]['assetId']])
            with sqlite3.connect(data / 'library/assets-v3.sqlite') as connection:
                self.assertNotEqual(connection.execute("SELECT value FROM metadata WHERE key='sources'").fetchone()[0], old_signature)
                self.assertIn('nightstand', connection.execute('SELECT text FROM search').fetchone()[0].split())
            self.assertEqual(catalog.read_bytes(), original)

    def test_native_readiness_note_distinguishes_unverified_from_unusable(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, _ = self.collection_fixture(Path(directory))
            result = assets(legacy, 'chair', root=data)
            for field in ('rigidReady', 'simulationReady', 'visualReady'):
                self.assertIs(result['items'][0][field], False)
                self.assertIn(field, result['note'])
            self.assertIn('尚未核验', result['note'])
            self.assertIn('不代表资产不可用或视觉质量差', result['note'])

    def test_exact_names_classify_unknown_source_categories_without_accessory_confusion(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory), count=3)
            for row, name in zip(document['assets'], ('Refrigerator042', 'Wardrobe011', 'CabinetDoor042')):
                row.update(name=name, description='Original imported source model.', category='Ikea_furniture')
            catalog.write_text(json.dumps(document))
            for category, index in (('refrigerator', 0), ('cabinet', 1)):
                with self.subTest(category=category):
                    found = assets(legacy, root=data, category=category)['items']
                    self.assertIn(document['assets'][index]['assetId'], [row['id'] for row in found])
            source = legacy / 'assets/ready_asset_manifest.json'
            source.parent.mkdir(parents=True)
            model = legacy / 'fixture.obj'
            model.write_text('v 0 0 0\n')
            source.write_text(json.dumps({'records': [{'asset_id': 'named-fridge', 'name': 'Refrigerator042',
                'description': 'Imported source model.', 'category': 'unknown_objects', 'visual_mesh_path': str(model)},
                {'asset_id': 'cabinet-lamp', 'name': 'cabinet with lamp', 'description': 'cabinet with table lamp',
                 'category': 'unknown_objects', 'visual_mesh_path': str(model)}]}))
            self.assertIn('named-fridge', [row['id'] for row in assets(legacy, root=data, category='refrigerator')['items']])
            self.assertIn('cabinet-lamp', [row['id'] for row in assets(legacy, root=data, category='cabinet')['items']])

    def test_joint_filter_requires_complete_same_revision_inspection_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory), count=7)
            complete = {'status': 'source_inspected', 'method': 'native_usd_joint_count', 'sourceRef': 'inspection.json',
                'sourceRevision': 'v1', 'sourceDigest': 'a' * 64}
            mutations = ({}, {'method': ''}, {'sourceRef': ''}, {'sourceRevision': 'v2'},
                         {'sourceDigest': 'not-a-digest'}, {'sourceDigest': True}, {'status': 'source_declared'})
            for row, changed in zip(document['assets'], mutations):
                row.update(movableJointCount=1, jointEvidence={**complete, **changed})
            document['assets'][0]['jointEvidence'] = {'status': 'source_inspected'}
            catalog.write_text(json.dumps(document))
            result = assets(legacy, root=data, requiresJoints=True)
            self.assertEqual(result['items'], [])
            self.assertEqual(result['jointFilter']['unknown'], 7)

    def test_missing_entry_does_not_consume_query_limit_but_remains_visible_by_id(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, document = self.collection_fixture(Path(directory), count=10)
            for row in document['assets'][:9]:
                (data / 'assets' / row['packageRoot'] / row['entry']).unlink()
            result = assets(legacy, 'chair', root=data, limit=1)
            self.assertEqual([row['id'] for row in result['items']], [document['assets'][-1]['assetId']])
            missing = assets(legacy, root=data, asset_id=document['assets'][0]['assetId'])['items'][0]
            self.assertFalse(missing['exists'])

    def test_complete_object_precedes_known_part_but_explicit_part_query_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory))
            document['assets'][0].update(name='cabinet door', description='cabinet door', category='cabinet', objectRole='part')
            catalog.write_text(json.dumps(document))
            source = legacy / 'assets/ready_asset_manifest.json'
            source.parent.mkdir(parents=True)
            model = legacy / 'cabinet.obj'
            model.write_text('v 0 0 0\n')
            source.write_text(json.dumps({'records': [{'asset_id': 'whole-cabinet', 'name': 'cabinet', 'category': 'cabinet',
                'description': 'cabinet', 'objectRole': 'whole', 'visual_mesh_path': str(model)}]}))
            self.assertEqual(assets(legacy, 'cabinet', root=data, limit=1)['items'][0]['id'], 'whole-cabinet')
            self.assertEqual(assets(legacy, 'cabinet door', root=data, limit=1)['items'][0]['id'], document['assets'][0]['assetId'])
            self.assertEqual(assets(legacy, root=data, asset_id=document['assets'][0]['assetId'])['items'][0]['objectRole'], 'part')

    def test_measured_preparation_fit_is_recalled_before_metadata_batch_is_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, data, _, report = self.prepared_fixture(root)
            _, _, catalog, _ = self.collection_fixture(root, count=12)
            # Twelve preferred unknown-size entries would fill the first verification batch.
            result = assets(legacy, 'chair', root=data, size=[1, 1, 1], limit=1)
            self.assertEqual(result['items'][0]['id'], 'native-chair')
            self.assertEqual(result['items'][0]['sizeMatch']['status'], 'fits')
            self.assertTrue(result['items'][0]['currentPreparation'])

    def test_progressive_checks_continue_when_verified_measurements_lower_the_ranking(self):
        from runtime.assets import _preparation_evidence
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, document = self.collection_fixture(Path(directory), count=12)
            count = 0
            def measured_after_check(*args, **kwargs):
                nonlocal count
                count += 1
                row = _preparation_evidence(*args, **kwargs)
                row.update(size=[3, 3, 3] if count <= 8 else [.5, .5, .5], sizeUnits='meters', sizeAxis='Z')
                return row
            with patch('runtime.assets._preparation_evidence', side_effect=measured_after_check):
                result = assets(legacy, 'chair', root=data, size=[1, 1, 1], limit=1)
            self.assertGreater(count, 8)
            self.assertEqual(result['items'][0]['sizeMatch']['status'], 'fits')

    def test_preferred_pool_is_recalled_before_the_broad_bm25_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, document = self.collection_fixture(Path(directory))
            source = legacy / 'assets/ready_asset_manifest.json'
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({'records': [{'asset_id': 'broad:' + str(index),
                'name': 'office chair', 'description': 'office chair ' * 20, 'category': 'chair',
                'visual_mesh_path': 'fixture.obj'} for index in range(1301)]}))
            result = assets(legacy, 'office chair', root=data, libraryTier='all', limit=1)
            self.assertEqual(result['items'][0]['id'], document['assets'][0]['assetId'])
            self.assertEqual(result['candidatePools']['preferred']['matched'], 1)
            self.assertTrue(result['candidatePools']['broad']['truncated'])

    def test_preferred_relevant_source_precedes_keyword_repetition(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, document = self.collection_fixture(Path(directory))
            source = legacy / 'assets/ready_asset_manifest.json'
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({'records': [{'asset_id': 'broad:exact', 'name': 'office chair',
                'description': 'office chair office chair', 'category': 'chair', 'visual_mesh_path': 'fixture.obj'}]}))
            result = assets(legacy, 'office chair', root=data)
            self.assertEqual(result['items'][0]['id'], document['assets'][0]['assetId'])

    def test_known_fit_survives_the_preferred_pool_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory), count=1202)
            for row in document['assets']:
                row.update(size=[3, 3, 3], units='meters', description='office chair ' * 20)
            wanted = document['assets'][-1]
            wanted.update(name='functional seat', description='A chair.', size=[.5, .5, .9])
            catalog.write_text(json.dumps(document))
            result = assets(legacy, 'office chair', root=data, libraryTier='preferred', size=[1, 1, 1], limit=1)
            self.assertEqual(result['items'][0]['id'], wanted['assetId'])
            self.assertEqual(result['items'][0]['sizeMatch']['status'], 'fits')
            self.assertTrue(result['candidatePools']['preferred']['truncated'])

    def test_known_fit_precedes_an_oversized_preferred_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory))
            document['assets'][0].update(size=[3, 3, 3], units='meters', description='office chair ' * 20)
            catalog.write_text(json.dumps(document))
            source = legacy / 'assets/ready_asset_manifest.json'
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({'records': [{'asset_id': 'fits', 'name': 'chair', 'category': 'chair',
                'dimensions': [.5, .5, .9], 'units': 'meters', 'up_axis': 'Z', 'visual_mesh_path': 'fixture.obj'}]}))
            result = assets(legacy, 'office chair', root=data, size=[1, 1, 1])
            self.assertEqual(result['items'][0]['id'], 'fits')

    def test_collection_and_required_joints_use_only_inspected_nonfixed_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory), count=5)
            evidence = {'status': 'source_inspected', 'method': 'fixture_native_joint_inspection',
                'sourceRef': 'fixture/joints.json', 'sourceRevision': 'v1', 'sourceDigest': 'a' * 64}
            document['assets'][0].update(jointCount=3, movableJointCount=2,
                jointTypes=['fixed', 'revolute', 'prismatic'], jointEvidence=evidence)
            document['assets'][1].update(jointCount=1, movableJointCount=0, jointTypes=['fixed'], jointEvidence=evidence)
            document['assets'][2].update(jointCount=3, sourceObjectType='Articulation')
            document['assets'][3].update(movableJointCount=True, jointEvidence=evidence)
            document['assets'][4].update(movableJointCount=2, jointEvidence={'status': 'source_declared'})
            document['collections'].append({**document['collections'][0], 'id': 'other-source'})
            document['assets'].append({**document['assets'][0], 'assetId': 'collection:other:v1:chair', 'collection': 'other-source'})
            catalog.write_text(json.dumps(document))
            result = assets(legacy, 'chair', root=data, collection='fixture-v1', requiresJoints=True)
            self.assertEqual([row['id'] for row in result['items']], [document['assets'][0]['assetId']])
            self.assertEqual(result['jointFilter']['unknown'], 3)
            self.assertEqual(result['jointFilter']['knownNonMovable'], 1)
            self.assertEqual(result['jointFilter']['knownMovable'], 1)
            plain = assets(legacy, 'chair', root=data, collection='fixture-v1', requiresJoints=False)
            self.assertEqual(len(plain['items']), 5)
            self.assertIsNone(next(row for row in plain['items'] if row['id'] == document['assets'][2]['assetId'])['movableJointCount'])
            self.assertIsNone(next(row for row in plain['items'] if row['id'] == document['assets'][3]['assetId'])['movableJointCount'])
            for changed in ({'collection': True}, {'requiresJoints': 1}):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    assets(legacy, root=data, **changed)

    def test_expensive_preparation_checks_are_bounded_and_progressively_fill(self):
        from runtime.assets import _preparation_evidence
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, _ = self.collection_fixture(Path(directory), count=40)
            with patch('runtime.assets._preparation_evidence', wraps=_preparation_evidence) as checked:
                result = assets(legacy, 'chair', root=data, category='chair', limit=3)
            self.assertEqual(len(result['items']), 3)
            self.assertLessEqual(checked.call_count, 8)
            count = 0
            def rejected_first_batch(*args, **kwargs):
                nonlocal count
                count += 1
                row = _preparation_evidence(*args, **kwargs)
                if count <= 8:
                    row['category'] = 'bottle'
                return row
            with patch('runtime.assets._preparation_evidence', side_effect=rejected_first_batch):
                result = assets(legacy, 'chair', root=data, category='chair', limit=3)
            self.assertEqual(len(result['items']), 3)
            self.assertGreater(count, 8)
            self.assertLessEqual(count, 16)

    def test_raw_matches_do_not_scan_files_or_decode_payloads_before_the_candidate_bound(self):
        from runtime.assets import _preparation_evidence, _read_report
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, _ = self.collection_fixture(Path(directory), count=2405)
            assets(legacy, 'chair', root=data, limit=3)
            is_file = Path.is_file
            file_checks = []
            def counted_file_check(path):
                file_checks.append(str(path))
                return is_file(path)
            with patch.object(Path, 'is_file', counted_file_check), \
                    patch('runtime.assets.json.loads', wraps=json.loads) as decoded, \
                    patch('runtime.assets._read_report', wraps=_read_report) as reports, \
                    patch('runtime.assets._preparation_evidence', wraps=_preparation_evidence) as prepared:
                result = assets(legacy, 'chair', root=data, limit=3)
            self.assertEqual(len(result['items']), 3)
            self.assertLessEqual(len(file_checks), 32)
            self.assertLessEqual(decoded.call_count, 1210)
            self.assertEqual(reports.call_count, 0)
            self.assertLessEqual(prepared.call_count, 8)
            self.assertTrue(result['candidatePools']['preferred']['truncated'])

    def test_prepared_measurement_override_survives_the_same_tier_candidate_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, data, _, report = self.prepared_fixture(root)
            manifest = legacy / 'assets/ready_asset_manifest.json'
            document = json.loads(manifest.read_text())
            document['records'] = [{'asset_id': 'aaa-chair-' + str(index), 'name': 'office chair',
                'description': 'office chair ' * 10, 'category': 'chair', 'visual_mesh_path': 'chair.glb',
                'units': 'unknown'} for index in range(1205)] + document['records']
            manifest.write_text(json.dumps(document))
            result = assets(legacy, 'office chair', root=data, size=[1, 1, 1], limit=1)
            self.assertEqual(result['items'][0]['id'], 'native-chair')
            self.assertEqual(result['items'][0]['sizeMatch']['status'], 'fits')
            self.assertTrue(result['items'][0]['currentPreparation'])
            self.assertTrue(result['candidatePools']['broad']['truncated'])

    def test_prepared_category_override_survives_the_same_tier_candidate_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, report = self.prepared_fixture(Path(directory), asset_id='imported:chair', name='Heritage chair')
            assets(legacy, 'chair', root=data)
            inputs = {**report['inputs'], 'attempt': 'current bowl identity'}
            digest = _json_hash(inputs)
            prepared = data / 'assets/prepared' / digest
            prepared.mkdir()
            for filename in report['dependencyFiles']:
                destination = prepared / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(report['reportPath']).parent / filename, destination)
            current = {**report, 'inputs': inputs, 'digest': digest, 'name': 'bowl', 'description': 'bowl',
                'category': 'bowl', 'reportPath': str(prepared / 'report.json'), 'path': str(prepared / 'visual.glb'),
                'thumbnailPath': str(prepared / 'thumbnail.png')}
            (prepared / 'report.json').write_text(json.dumps(current))
            _register(data, current)
            manifest = legacy / 'assets/ready_asset_manifest.json'
            document = json.loads(manifest.read_text())
            document['records'] += [{'asset_id': 'aaa-bowl-' + str(index), 'name': 'bowl', 'description': 'bowl',
                'category': 'bowl', 'visual_mesh_path': 'chair.glb'} for index in range(1205)]
            manifest.write_text(json.dumps(document))
            result = assets(legacy, 'bowl', root=data, category='bowl', limit=1)
            self.assertEqual(result['items'][0]['id'], 'imported:chair')
            self.assertEqual(result['items'][0]['category'], 'bowl')
            self.assertTrue(result['items'][0]['currentPreparation'])
            self.assertTrue(result['candidatePools']['broad']['truncated'])

    def test_semantic_class_and_size_take_precedence_over_preferred_library(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, catalog, document = self.collection_fixture(Path(directory))
            document['assets'][0].update(name='chair accessory', description='tool attachment used with chair',
                category='tool', size=[2, 2, 2])
            catalog.write_text(json.dumps(document))
            source = legacy / 'assets/ready_asset_manifest.json'
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({'records': [{'asset_id': 'matching-chair', 'name': 'office chair',
                'description': 'office chair', 'category': 'chair', 'dimensions': [.5, .5, 1],
                'units': 'meters', 'visual_mesh_path': 'fixture.obj'}]}))
            result = assets(legacy, 'office chair', root=data, libraryTier='all', size=[1, 1, 1])
            self.assertEqual(result['items'][0]['id'], 'matching-chair')
            self.assertEqual(assets(legacy, 'chair', root=data, category='chair')['items'][0]['id'], 'matching-chair')

    def test_collection_changes_and_declared_source_roots_refresh_the_same_index(self):
        for source_root in ('current_assets', 'legacy_assets', 'legacy_data'):
            with self.subTest(source_root=source_root), tempfile.TemporaryDirectory() as directory:
                legacy, data, catalog, document = self.collection_fixture(Path(directory), source_root=source_root)
                self.assertEqual(assets(legacy, 'office chair', root=data)['items'][0]['libraryTier'], 'preferred')
                document['assets'][0].update(name='laboratory stool', description='laboratory stool')
                catalog.write_text(json.dumps(document))
                self.assertEqual(assets(legacy, 'laboratory', root=data)['items'][0]['name'], 'laboratory stool')
                self.assertEqual(len(list((data / 'library').glob('*.sqlite'))), 1)

    def test_collection_paths_reject_escape_symlink_and_project_redirection(self):
        for changed in ({'packageRoot': '../projects/private'}, {'entry': '/tmp/other.usd'},
                        {'dependencyFiles': ['../private.bin']}, {'sourceRoot': 'project'}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                legacy, data, catalog, document = self.collection_fixture(Path(directory))
                document['assets'][0].update(changed)
                catalog.write_text(json.dumps(document))
                with self.assertRaisesRegex(ValueError, 'collection|relative|source root|escape'):
                    assets(legacy, root=data)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, data, catalog, document = self.collection_fixture(root)
            package = data / 'assets' / document['assets'][0]['packageRoot']
            shutil.rmtree(package)
            private = data / 'projects/private'
            private.mkdir(parents=True)
            (private / 'model.usda').write_text('#usda 1.0')
            package.symlink_to(private, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'collection|symlink|source root'):
                assets(legacy, root=data)

    def prepared_fixture(self, root, *, asset_id='native-chair', versions=None, name='prepared-file', register=True):
        legacy, data = root / 'legacy', root / 'data'
        model = legacy / 'chair.glb'
        model.parent.mkdir(parents=True, exist_ok=True)
        document = json.dumps({'asset': {'version': '2.0'}, 'meshes': []}).encode()
        document += b' ' * (-len(document) % 4)
        if not model.exists():
            model.write_bytes(struct.pack('<4sIIII', b'glTF', 2, len(document) + 20, len(document), 0x4E4F534A) + document)
        catalog = legacy / 'assets/ready_asset_manifest.json'
        catalog.parent.mkdir(exist_ok=True)
        catalog.write_text(json.dumps({'records': [{'asset_id': 'native-chair', 'name': 'catalog chair',
            'description': 'A black upholstery office chair.', 'category': 'chair', 'aliases': ['task seating'],
            'visual_mesh_path': str(model), 'up_axis': 'Z', 'units': 'meters', 'license': 'CC0'}]}))
        source_item = next(_source_rows(legacy, None))
        inputs = {'versions': versions if versions is not None else _versions(), 'assetId': asset_id,
                  'dependencies': [{'path': str(model.resolve()), 'sha256': hashlib.sha256(model.read_bytes()).hexdigest(),
                                    'bytes': model.stat().st_size}], 'sourceMetadata': source_item}
        digest = _json_hash(inputs)
        directory = data / 'assets/prepared' / digest
        directory.mkdir(parents=True, exist_ok=True)
        source = {'kind': 'online_import' if asset_id.startswith('imported:') else 'legacy',
                  'licenseEvidence': {'status': 'source_index_declaration', 'value': 'CC0'}}
        files = {'visual.glb': model.read_bytes(), 'source.json': json.dumps(source).encode(),
                 'thumbnail.png': b'actual thumbnail fixture', 'provenance/input/chair.glb': model.read_bytes()}
        for filename, content in files.items():
            path = directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        report = {'assetId': asset_id, 'name': name, 'description': 'prepared chair', 'category': 'chair',
                  'path': str(directory / 'visual.glb'), 'reportPath': str(directory / 'report.json'),
                  'digest': digest, 'inputs': inputs, 'source': source, 'kind': 'static_mesh',
                  'upAxis': 'Z', 'units': 'meters', 'size': [.5, .6, .9], 'sizeUnits': 'meters', 'license': 'CC0',
                  'collisionFidelity': 'bounding_box', 'articulationPrepared': False,
                  'textureEvidence': {'imageCount': 1, 'uvVerified': True, 'status': 'prepared_texture_uv'},
                  'thumbnail': {'status': 'rendered', 'path': 'thumbnail.png'},
                  'thumbnailPath': str(directory / 'thumbnail.png'), 'dependencyFiles': list(files),
                  'preparedFiles': ['report.json', *files],
                  'frozenDigests': {filename: hashlib.sha256(content).hexdigest() for filename, content in files.items()}}
        (directory / 'report.json').write_text(json.dumps(report))
        if register:
            _register(data, report)
        return legacy, data, model, report

    def fixture(self, root):
        legacy = root / 'legacy'
        metadata = legacy / 'data/objathor-assets/preprocessed/metadata_index.json'
        metadata.parent.mkdir(parents=True)
        (legacy / 'fixture.obj').write_text('v 0 0 0\n')
        metadata.write_text(json.dumps({
            'chair': {'name': 'Unknown', 'description': 'A black upholstery office chair.',
                      'category': 'large_objects', 'bounding_box': [0, 0, 0]},
            'lamp': {'name': 'Unknown', 'description': 'A wooden table lamp.',
                     'category': 'large_objects', 'bounding_box': [0, 0, 0]},
            'table': {'name': 'Unknown', 'description': 'A wooden table with four legs.',
                      'category': 'large_objects', 'bounding_box': [0, 0, 0]},
        }))
        for uid in ('chair', 'lamp', 'table'):
            path = legacy / f'data/objathor-assets/assets/{uid}/{uid}.pkl.gz'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'prefab read only at preparation')
        return legacy

    def test_source_semantics_recall_office_chair_and_unknown_dimensions_stay_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self.fixture(root)
            result = assets(legacy, '办公椅', root=root / 'data')
            self.assertEqual(result['matched'], 1)
            item = result['items'][0]
            self.assertEqual(item['id'], 'objathor:chair')
            self.assertIn('office chair', item['description'])
            self.assertIsNone(item['size'])
            self.assertEqual(item['measuredSize']['status'], 'unknown')

    def test_local_preparation_recovers_measurement_without_replacing_catalog_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, model, report = self.prepared_fixture(Path(directory))
            item = assets(legacy, 'task seating', root=data, size=[.7, .7, 1])['items'][0]
            self.assertEqual(item['id'], 'native-chair')
            self.assertEqual(item['name'], 'catalog chair')
            self.assertIn('office chair', item['description'])
            self.assertEqual(item['aliases'], ['task seating'])
            self.assertTrue(item['currentPreparation'])
            self.assertTrue(item['rigidReady'])
            self.assertEqual(item['measuredSize']['status'], 'measured')
            self.assertEqual(item['sizeMatch']['status'], 'fits')
            self.assertEqual(item['thumbnailPath'], report['thumbnailPath'])
            self.assertEqual(item['collisionFidelity'], 'bounding_box')
            self.assertEqual(model.read_bytes(), (Path(report['reportPath']).parent / 'provenance/input/chair.glb').read_bytes())

    def test_local_preparation_is_recovered_after_disposable_index_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, report = self.prepared_fixture(Path(directory), register=False)
            first = assets(legacy, 'office chair', root=data)['items'][0]
            (data / 'library/assets-v3.sqlite').unlink()
            second = assets(legacy, 'office chair', root=data)['items'][0]
            self.assertEqual(first['reportPath'], report['reportPath'])
            self.assertEqual(first, second)

    def test_runtime_version_change_keeps_catalog_index_but_invalidates_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, _ = self.prepared_fixture(Path(directory), versions={'algorithm': 'old'})
            with patch('runtime.assets._versions', return_value={'algorithm': 'old'}):
                self.assertTrue(assets(legacy, 'office chair', root=data)['items'][0]['rigidReady'])
            with patch('runtime.assets._versions', return_value={'algorithm': 'new'}), patch(
                    'runtime.assets._source_rows', side_effect=AssertionError('unchanged catalog rebuilt')):
                item = assets(legacy, 'office chair', root=data)['items'][0]
            self.assertFalse(item['rigidReady'])
            self.assertFalse(item['currentPreparation'])
            self.assertEqual(item['preparationStatus'], 'stale')

    def test_catalog_signature_change_still_rebuilds_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self.fixture(root)
            self.assertEqual(assets(legacy, 'office chair', root=root / 'data')['matched'], 1)
            metadata = legacy / 'data/objathor-assets/preprocessed/metadata_index.json'
            document = json.loads(metadata.read_text())
            document['chair']['description'] = 'A rotating laboratory seat.'
            metadata.write_text(json.dumps(document))
            self.assertEqual(assets(legacy, 'laboratory', root=root / 'data')['items'][0]['id'], 'objathor:chair')

    def test_changed_source_and_tampered_frozen_model_are_not_current_preparations(self):
        for changed in ('source', 'frozen'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                legacy, data, model, report = self.prepared_fixture(Path(directory))
                self.assertTrue(assets(legacy, 'office chair', root=data)['items'][0]['rigidReady'])
                if changed == 'source':
                    model.write_bytes(model.read_bytes() + b'changed')
                else:
                    Path(report['path']).write_bytes(b'changed frozen model')
                item = assets(legacy, 'office chair', root=data)['items'][0]
                self.assertFalse(item['currentPreparation'])
                self.assertFalse(item['rigidReady'])
                self.assertEqual(item['measuredSize']['status'], 'unknown')
                self.assertFalse(item['textureEvidence']['uvVerified'])

    def test_stale_newer_report_does_not_mask_current_imported_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, data, _, current = self.prepared_fixture(root, asset_id='imported:chair', name='current office chair')
            _, _, _, stale = self.prepared_fixture(root, asset_id='imported:chair', name='stale office chair',
                                                   versions={'algorithm': 'old'})
            os.utime(current['reportPath'], (100, 100))
            os.utime(stale['reportPath'], (200, 200))
            item = assets(legacy, 'office chair', root=data, asset_id='imported:chair')['items'][0]
            self.assertTrue(item['rigidReady'])
            self.assertEqual(item['name'], 'current office chair')
            self.assertEqual(item['reportPath'], current['reportPath'])

    def test_bad_report_cannot_replace_searchable_generated_semantics_or_category(self):
        for failure in ('missing_file', 'wrong_digest', 'incomplete_manifest', 'changed_bytes', 'wrong_frozen_hash'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                legacy, data, _, report = self.prepared_fixture(Path(directory), asset_id='generated:chair', name='office chair')
                self.assertTrue(assets(legacy, 'office chair', root=data, asset_id=report['assetId'])['items'][0]['rigidReady'])
                inputs = {**report['inputs'], 'attempt': failure}
                digest = _json_hash(inputs)
                broken = data / 'assets/prepared' / digest
                broken.mkdir()
                for filename in report['dependencyFiles']:
                    destination = broken / filename
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(Path(report['reportPath']).parent / filename, destination)
                changed = {**report, 'inputs': inputs, 'digest': digest, 'name': 'bottle', 'description': 'bottle',
                           'category': 'bottle', 'reportPath': str(broken / 'report.json'), 'path': str(broken / 'visual.glb'),
                           'thumbnailPath': str(broken / 'thumbnail.png')}
                if failure == 'missing_file':
                    (broken / 'visual.glb').unlink()
                elif failure == 'wrong_digest':
                    changed['digest'] = '0' * 64
                elif failure == 'incomplete_manifest':
                    changed['frozenDigests'] = {name: value for name, value in report['frozenDigests'].items() if name != 'source.json'}
                    changed['dependencyFiles'] = [name for name in report['dependencyFiles'] if name != 'source.json']
                    changed['preparedFiles'] = [name for name in report['preparedFiles'] if name != 'source.json']
                elif failure == 'changed_bytes':
                    (broken / 'visual.glb').write_bytes(b'changed geometry')
                else:
                    changed['frozenDigests'] = {**report['frozenDigests'], 'visual.glb': '0' * 64}
                (broken / 'report.json').write_text(json.dumps(changed))
                _register(data, changed)
                office = assets(legacy, 'office chair', root=data, category='chair')['items']
                selected = next((item for item in office if item['id'] == report['assetId']), None)
                self.assertIsNotNone(selected)
                self.assertTrue(selected['rigidReady'])
                self.assertEqual(selected['name'], 'office chair')
                self.assertEqual(assets(legacy, 'bottle', root=data)['items'], [])
                self.assertEqual(assets(legacy, 'bottle', root=data, category='bottle')['items'], [])
                self.assertEqual(assets(legacy, root=data, asset_id=report['assetId'], category='bottle')['items'], [])

    def test_valid_current_report_rename_preserves_previous_name_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, report = self.prepared_fixture(Path(directory), asset_id='imported:chair', name='Heritage Task Seat')
            assets(legacy, 'Heritage', root=data)
            inputs = {**report['inputs'], 'attempt': 'valid rename'}
            digest = _json_hash(inputs)
            prepared = data / 'assets/prepared' / digest
            prepared.mkdir()
            for filename in report['dependencyFiles']:
                destination = prepared / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(report['reportPath']).parent / filename, destination)
            current = {**report, 'inputs': inputs, 'digest': digest, 'name': 'Modern Adjustable Office Chair',
                       'description': 'Modern Adjustable Office Chair', 'reportPath': str(prepared / 'report.json'),
                       'path': str(prepared / 'visual.glb'), 'thumbnailPath': str(prepared / 'thumbnail.png')}
            (prepared / 'report.json').write_text(json.dumps(current))
            _register(data, current)
            item = assets(legacy, 'Heritage', root=data)['items'][0]
            self.assertEqual(item['id'], report['assetId'])
            self.assertTrue(item['rigidReady'])
            self.assertEqual(item['name'], current['name'])
            self.assertEqual(item['reportPath'], current['reportPath'])

    def test_prepared_synonym_and_verified_historical_name_use_the_same_search_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, report = self.prepared_fixture(Path(directory), asset_id='imported:seat', name='Heritage couch')
            report.update(description='Original source model.')
            Path(report['reportPath']).write_text(json.dumps(report))
            for query in ('couch', 'sofa'):
                found = assets(legacy, query, root=data)['items']
                self.assertEqual([item['id'] for item in found], [report['assetId']])
                self.assertTrue(found[0]['currentPreparation'])
            inputs = {**report['inputs'], 'attempt': 'valid rename'}
            digest = _json_hash(inputs)
            prepared = data / 'assets/prepared' / digest
            prepared.mkdir()
            for filename in report['dependencyFiles']:
                destination = prepared / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(report['reportPath']).parent / filename, destination)
            current = {**report, 'inputs': inputs, 'digest': digest, 'name': 'Modern lounge seat',
                       'description': 'Modern lounge seat', 'reportPath': str(prepared / 'report.json'),
                       'path': str(prepared / 'visual.glb'), 'thumbnailPath': str(prepared / 'thumbnail.png')}
            Path(current['reportPath']).write_text(json.dumps(current))
            _register(data, current)
            for query in ('couch', 'sofa'):
                item = assets(legacy, query, root=data)['items'][0]
                self.assertEqual(item['id'], report['assetId'])
                self.assertEqual(item['name'], current['name'])
                self.assertEqual(item['reportPath'], current['reportPath'])
                self.assertTrue(item['currentPreparation'])
                self.assertIn('Heritage couch', item['aliases'])
            self.assertEqual(assets(legacy, 'sofa', root=data, category='sofa')['items'], [])

    def test_stale_lookup_report_is_rejected_before_hashing_its_frozen_files(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, _ = self.prepared_fixture(Path(directory), versions={'algorithm': 'old'})
            with patch('runtime.assets._cache_valid', side_effect=AssertionError('stale candidate files were hashed')):
                item = assets(legacy, 'office chair', root=data)['items'][0]
            self.assertFalse(item['rigidReady'])
            self.assertEqual(item['preparationStatus'], 'stale')

    def test_broken_report_does_not_block_other_catalog_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, data, _, _ = self.prepared_fixture(Path(directory))
            broken = data / 'assets/prepared/broken/report.json'
            broken.parent.mkdir()
            broken.write_text('{broken')
            self.assertEqual(assets(legacy, 'office chair', root=data)['items'][0]['id'], 'native-chair')

    def test_malformed_report_fields_do_not_break_other_catalog_candidates(self):
        for changed in ({'assetId': 42}, {'inputs': []}, {'inputs': {'versions': []}},
                        {'name': {}}, {'textureEvidence': []}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                legacy, data, _, report = self.prepared_fixture(Path(directory))
                broken = data / 'assets/prepared/broken/report.json'
                broken.parent.mkdir()
                broken.write_text(json.dumps({**report, 'assetId': 'imported:broken', **changed}))
                item = assets(legacy, 'office chair', root=data)['items'][0]
                self.assertEqual(item['id'], 'native-chair')
                self.assertTrue(item['rigidReady'])

    def test_object_class_does_not_match_a_table_lamp_as_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self.fixture(root)
            self.assertEqual(assets(legacy, 'table', root=root / 'data')['items'][0]['id'], 'objathor:table')
            self.assertEqual(assets(legacy, 'lamp', root=root / 'data')['items'][0]['id'], 'objathor:lamp')

    def test_trusted_cabinet_category_is_not_overridden_by_accessory_lamp_words(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self.fixture(root)
            path = legacy / 'assets/ready_asset_manifest.json'
            path.parent.mkdir(parents=True)
            model = legacy / 'cabinet.obj'
            model.write_text('v 0 0 0')
            path.write_text(json.dumps({'records': [{'asset_id': 'cabinet', 'name': 'cabinet',
                'description': 'A cabinet with a table lamp on top.', 'category': 'cabinet', 'visual_mesh_path': str(model)}]}))
            result = assets(legacy, 'cabinet', root=root / 'data')
            self.assertEqual(result['items'][0]['id'], 'cabinet')
            self.assertEqual(result['items'][0]['category'], 'cabinet')

    def test_empty_generated_category_remains_retrievable_without_a_class_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self.fixture(root)
            directory = root / 'data/assets/prepared/example'
            directory.mkdir(parents=True)
            model = directory / 'visual.glb'
            model.write_bytes(b'frozen mesh')
            report = {'assetId': 'generated:chair', 'name': 'custom office chair', 'description': 'office chair',
                      'category': '', 'kind': 'static_mesh', 'path': str(model),
                      'reportPath': str(directory / 'report.json'), 'source': {'kind': 'native_code_generated'}}
            (directory / 'report.json').write_text(json.dumps(report))
            items = assets(legacy, 'office chair', root=root / 'data')['items']
            self.assertIn('generated:chair', [item['id'] for item in items])
            generated = next(item for item in items if item['id'] == 'generated:chair')
            self.assertEqual(generated['category'], 'chair')
            self.assertFalse(generated['rigidReady'])
            self.assertFalse(generated['currentPreparation'])

    def test_unclassified_match_is_retained_as_fallback_and_explicit_category_is_hard_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self.fixture(root)
            path = legacy / 'assets/ready_asset_manifest.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'records': [{'asset_id': 'unclassified', 'name': 'support for chair',
                'description': 'Tool attachment used with chair', 'category': 'tool', 'visual_mesh_path': 'fixture.obj'}]}))
            items = assets(legacy, 'chair', root=root / 'data')['items']
            self.assertIn('unclassified', [item['id'] for item in items])
            self.assertEqual(items[0]['id'], 'objathor:chair')
            explicit = assets(legacy, 'chair', root=root / 'data', category='chair')['items']
            self.assertNotIn('unclassified', [item['id'] for item in explicit])

    def test_hssd_support_annotation_is_not_a_furniture_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self.fixture(root)
            index = legacy / 'data/hssd-models/sceneagent-index.json'
            index.parent.mkdir(parents=True)
            index.write_text(json.dumps({'support-surfaces': [{
                'uid': 'hssd:chair.supportSurface', 'name': 'wood chair',
                'metadata': {'hssd_sidecar_kind': 'supportSurface'},
                'glb_path': 'chair.supportSurface.glb', 'source': 'hssd'}]}))
            items = assets(legacy, 'chair', root=root / 'data')['items']
            self.assertEqual([item['id'] for item in items], ['objathor:chair'])


if __name__ == '__main__':
    unittest.main()
