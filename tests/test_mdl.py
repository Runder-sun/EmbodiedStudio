"""SYSTEM_VERIFICATION_ONLY: MDL file identities, never a generated product."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime import mdl


class MdlIdentityTests(unittest.TestCase):
    def test_same_bytes_cannot_hide_a_different_unqualified_import_location(self):
        expected = {'package/a/main.mdl':'same bytes'}
        mdl.verify_source_identity({'key':'package/a/main.mdl','sha256':'same bytes'},
                                   'package/a/main.mdl', expected)
        with self.assertRaisesRegex(ValueError,'alias|location'):
            mdl.verify_source_identity({'key':'package/b/main.mdl','sha256':'same bytes'},
                                       'package/a/main.mdl', expected)
        with self.assertRaisesRegex(ValueError,'changed|digest'):
            mdl.verify_source_identity({'key':'package/a/main.mdl','sha256':'changed bytes'},
                                       'package/a/main.mdl', expected)

    def test_relocation_preserves_import_edges_and_file_identity_not_encoded_db_names(self):
        first = {'modules': [{'key':'package/a.mdl','sha256':'a','imports':['package/b.mdl'],
                             'dbName':'mdl::encoded_old'},
                            {'key':'package/b.mdl','sha256':'b','imports':[]}],
                 'resources': {'package/t.png':'t'}}
        moved = json.loads(json.dumps(first))
        moved['modules'][0]['dbName'] = 'mdl::encoded_new'
        self.assertEqual(mdl.semantic(first), mdl.semantic(moved))
        moved['modules'][0]['imports'] = []
        self.assertNotEqual(mdl.semantic(first), mdl.semantic(moved))

    def test_uri_identity_rejects_network_and_escape_and_hashes_real_bytes(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root/'material.mdl').write_bytes(b'original')
            record = mdl.file_identity((root/'material.mdl').as_uri(), root)
            self.assertEqual(record['key'], 'package/material.mdl')
            self.assertEqual(record['sha256'], hashlib.sha256(b'original').hexdigest())
            for value in ('https://example.com/t.png', 'file://other-host/a.mdl', '/outside.mdl'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    mdl.file_identity(value, root)

    def test_module_roots_come_only_from_frozen_report_inputs(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            package = root/'dependencies/resources/fixture'
            (package/'files/vendor').mkdir(parents=True)
            (package/'files/vendor/a.mdl').write_text('mdl 1.7;')
            (package/'report.json').write_text(json.dumps({'kind':'native_files',
                'nativeFiles':['files/vendor/a.mdl'], 'inputs':{'mdlSearchRoots':['files/vendor']}}))
            self.assertEqual(mdl.declared_roots(root), ['dependencies/resources/fixture/files/vendor'])
            (package/'report.json').write_text(json.dumps({'kind':'native_files',
                'inputs':{'mdlSearchRoots':['../../outside']}}))
            with self.assertRaises(ValueError):
                mdl.declared_roots(root)

    def test_manifest_precheck_is_pure_json_and_detects_package_and_sdk_mutation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root/'material.mdl'; path.write_bytes(b'original')
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            audit = {'schema':'sceneagent-native-stage-audit-v2','files':{'material.mdl':checksum},
                     'mdl':{'schema':'sceneagent-mdl-audit-v1','roots':[], 'sdk':{'fixture':'v1'},
                            'files':{'package/material.mdl':checksum},'modules':[], 'resources':{},'entries':[]}}
            with patch.object(mdl, 'sdk_identity', return_value={'fixture':'v1'}):
                mdl.verify(audit, root)
                path.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError,'changed|digest|identity'):
                    mdl.verify(audit,root)
            path.write_bytes(b'original')
            with patch.object(mdl, 'sdk_identity', return_value={'fixture':'v2'}), self.assertRaisesRegex(ValueError,'SDK'):
                mdl.verify(audit,root)


if __name__ == '__main__':
    unittest.main()
