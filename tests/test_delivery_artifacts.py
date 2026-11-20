"""SYSTEM_VERIFICATION_ONLY: producer file registration, not generated products."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.read_delivery import freeze, verify
from examples import read_delivery


class DeliveryArtifactsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'video decoder unavailable')
    def test_video_only_reads_full_source_video_without_training_formats_and_rejects_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, source, pointer, binding, episodes = self.fixture(Path(temporary))
            binding.update(requiredOutputs=['demonstrations'], environmentRefs=[dict(revision='rev_test')], videoOnly=True)
            episodes[0]['revision'] = 'rev_test'
            original = project/'episodes/ep_test/video.mp4'; original.parent.mkdir(parents=True)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=32x32:r=10',
                '-t', '0.4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(original)], check=True)
            shutil.copyfile(original, source/'video.mp4')
            mapping = dict(episodes=[dict(episodes[0], sourceVideo='episodes/ep_test/video.mp4')])
            (source/'mapping.json').write_text(json.dumps(mapping))
            manifest = json.loads((source/'manifest.json').read_text())
            manifest['binding'] = binding; manifest['episodes'] = episodes
            manifest['products'] = dict(robot=[dict(video='video.mp4', mapping='mapping.json')])
            self.publish(source, pointer, manifest)
            frozen, manifest = freeze(project, pointer, binding, episodes)
            result = read_delivery.read_formats(frozen, manifest, '', project=project)
            self.assertEqual(result['robot'][0]['frames'], 4)
            self.assertNotIn('lerobot', result['libraries'])
            (frozen/'video.mp4').write_bytes(b'not the original video')
            with self.assertRaises(ValueError): read_delivery.read_formats(frozen, manifest, '', project=project)

    def test_training_container_rejects_nonmessages_and_preserves_real_tools_and_multimodal_parts(self):
        refs = {'event1'}
        valid = [dict(messages=[dict(role='user', content='question'), dict(role='assistant', content='answer')]),
                 dict(messages=[dict(role='user',content='question',tool_calls=[]),dict(role='assistant',content='answer',tool_calls=[])]),
                 dict(messages=[dict(role='assistant', content=None, tool_calls=[dict(type='function',
                    function=dict(name='inspect', arguments={'id':'source'}))])]),
                 dict(messages=[dict(role='user', content=[dict(type='text', text='see'),
                    dict(type='image_url', image_url={'url':'image.png'})])]),
                 dict(messages=[dict(role='user',content='x')], tools=[dict(type='function',function=dict(name='inspect',parameters={}))]),
                 dict(prompt='known input', completion='target')]
        invalid = [dict(messages=[1]), dict(messages=[{}]), dict(messages=[dict(role='invalid',content='x')]),
                   dict(messages=[dict(role='user',content=123)]), dict(messages=[dict(role='user',content=[1])]),
                   dict(messages=[dict(role='assistant',tool_calls=[dict(function={'arguments':'{}'})])]),
                   dict(messages=[dict(role='user',content=[dict(type='text',text=3)])]),
                   dict(messages=[dict(role='user',content='x')],tools=[1]),
                   dict(messages=[dict(role='user',content=[dict(type='image_url',image_url=1)])])]
        for row in valid:
            read_delivery.validate_training_row(dict(row, sourceEventIds=['event1']), refs)
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                read_delivery.validate_training_row(dict(row, sourceEventIds=['event1']), refs)

    def fixture(self, root):
        project = root/'projects/p_test'
        source = project/'delivery/run'
        source.mkdir(parents=True)
        raw = b'{"eventId":"p_test:1","generationId":"g_test"}\n'
        (project/'generation-trace.jsonl').write_bytes(raw+b'{"later":true}\n')
        (source/'raw.jsonl').write_bytes(raw)
        (source/'binary.bin').write_bytes(bytes(range(256))*8192)
        binding = dict(projectId='p_test', generationId='g_test', requirementVersion=1,
                       requestHash='a'*64, sourceHash='b'*64, revision='rev_test')
        episodes = [dict(episodeId='ep_test', taskId='test', use='execution_data')]
        manifest = dict(schema='sceneagent-produced-artifacts-v1', binding=binding, episodes=episodes,
            trace=dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()),
            files=[dict(path=p.name, bytes=p.stat().st_size, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                   for p in sorted(source.iterdir())],
            products=dict(generation=dict(rawTrace='raw.jsonl', atif='atif.json', training='training.jsonl'),
                          robot=[dict(lerobot='robot', hdf5='data.hdf5', mapping='mapping.json')],
                          environment=dict(root='environment', entrypoint='evaluate.py', readme='README.md', runtime='runtime.json')))
        data = json.dumps(manifest).encode()
        (source/'manifest.json').write_bytes(data)
        pointer = dict(manifestRef='projects/p_test/delivery/run/manifest.json', sha256=hashlib.sha256(data).hexdigest())
        return project, source, pointer, binding, episodes

    def publish(self, source, pointer, manifest):
        manifest['files'] = [dict(path=p.relative_to(source).as_posix(), bytes=p.stat().st_size,
            sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(source.rglob('*'))
            if p.is_file() and p.name != 'manifest.json']
        data = json.dumps(manifest, ensure_ascii=False).encode()
        (source/'manifest.json').write_bytes(data)
        pointer['sha256'] = hashlib.sha256(data).hexdigest()

    def archive_fixture(self, root):
        project, source, pointer, binding, episodes = self.fixture(root)
        refs = []
        def archive(kind, value):
            data = ('\n'+json.dumps(value, ensure_ascii=False, indent=2)+' \n').encode()
            sha = (value['sourceHash'] if kind == 'sources' else hashlib.sha256(data).hexdigest())
            ref = '.trace/'+kind+'/'+sha+'.json'
            for base in (project, source/'archives'):
                path = base/ref; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            refs.append(ref)
            return ref, sha
        old_files = {'scene.py': '# 中文历史源\nVALUE = "  空白  "\n'}
        old_hash = hashlib.sha256(json.dumps(old_files, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        old_ref, _ = archive('sources', dict(sourceHash=old_hash, sourceFiles=old_files))
        instructions, _ = archive('inputs', dict(kind='thread_instructions', text='保留中文与空白  \n'))
        input_ref, input_hash = archive('inputs', dict(kind='native_turn_input', instructionsRef=instructions, text='真实输入'))
        files = dict(old_files, **{'helper.py': 'VALUE = 2\n'})
        sha = hashlib.sha256(json.dumps(files, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        ref, _ = archive('sources', dict(sourceHash=sha, sourceFiles=files))
        rows = [dict(type='source_snapshot', sourceHash=old_hash, snapshotRef=old_ref, generationId='g_old'),
                dict(type='input_snapshot', inputHash=input_hash, snapshotRef=input_ref),
                dict(type='source_snapshot', sourceHash=sha, snapshotRef=ref, previousSourceHash=old_hash)]
        for i, row in enumerate(rows): row.update(eventId='p_test:'+str(i+1), generationId=row.get('generationId', 'g_test'))
        raw = b''.join(json.dumps(row).encode()+b'\n' for row in rows)
        (source/'raw.jsonl').write_bytes(raw)
        (project/'generation-trace.jsonl').write_bytes(raw+b'{"snapshotRef":".trace/inputs/'+b'f'*64+b'.json"}\n')
        manifest = json.loads((source/'manifest.json').read_text())
        manifest['trace'] = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        manifest['products']['generation']['archiveRoot'] = 'archives'
        self.publish(source, pointer, manifest)
        return project, source, pointer, binding, episodes, refs

    def test_generation_raw_requires_original_archives_even_with_legacy_default_outputs(self):
        for missing in ('archives', 'archiveRoot', 'generation'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                project, source, pointer, binding, episodes, refs = self.archive_fixture(Path(temporary))
                manifest = json.loads((source/'manifest.json').read_text())
                if missing == 'archives': shutil.rmtree(source/'archives')
                elif missing == 'archiveRoot': manifest['products']['generation'].pop('archiveRoot')
                else: manifest['products'].pop('generation')
                self.publish(source, pointer, manifest)
                with self.assertRaises(ValueError): freeze(project, pointer, binding, episodes)

    def test_complete_archive_freeze_verifies_original_bytes_and_restores_without_active_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, source, pointer, binding, episodes, refs = self.archive_fixture(Path(temporary))
            frozen, manifest = freeze(project, pointer, binding, episodes)
            for ref in refs: self.assertEqual((frozen/'archives'/ref).read_bytes(), (project/ref).read_bytes())
            source_archive = json.loads((frozen/'archives'/refs[0]).read_bytes())
            self.assertNotEqual(source_archive['sourceHash'], hashlib.sha256((frozen/'archives'/refs[0]).read_bytes()).hexdigest())
            shutil.rmtree(project/'.trace'); shutil.rmtree(source); (project/'generation-trace.jsonl').unlink()
            detached = Path(temporary)/'detached'; shutil.move(frozen, detached)
            self.assertEqual(verify(detached, pointer['sha256'], binding, episodes), manifest)
            (detached/'archives'/refs[-1]).unlink()
            with self.assertRaises(ValueError): verify(detached, pointer['sha256'], binding, episodes)

    def test_archive_closure_rejects_reencoded_original_wrong_input_hash_and_missing_instruction(self):
        for failure in ('reencoded', 'input_hash', 'declaration', 'instructions', 'previous_source'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                project, source, pointer, binding, episodes, refs = self.archive_fixture(Path(temporary))
                manifest = json.loads((source/'manifest.json').read_text())
                if failure == 'reencoded':
                    path = source/'archives'/refs[0]
                    path.write_text(json.dumps(json.loads(path.read_bytes()), ensure_ascii=False))
                elif failure == 'input_hash':
                    path = source/'archives'/refs[2]; path.write_text('{"text":"tampered"}')
                elif failure == 'instructions': (source/'archives'/refs[1]).unlink()
                else:
                    rows = [json.loads(line) for line in (source/'raw.jsonl').read_text().splitlines()]
                    if failure == 'declaration': rows[1]['inputHash'] = '0'*64
                    else:
                        (source/'archives'/refs[0]).unlink()
                        rows = rows[1:]
                    raw = b''.join(json.dumps(row).encode()+b'\n' for row in rows)
                    (source/'raw.jsonl').write_bytes(raw); (project/'generation-trace.jsonl').write_bytes(raw)
                    manifest['trace'] = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                self.publish(source, pointer, manifest)
                with self.assertRaises(ValueError): freeze(project, pointer, binding, episodes)

    def test_empty_archive_closure_does_not_require_an_archive_root(self):
        for archive_root in (None, 'unused_empty_archives'):
            with self.subTest(root=archive_root), tempfile.TemporaryDirectory() as temporary:
                project, source, pointer, binding, episodes = self.fixture(Path(temporary))
                manifest = json.loads((source/'manifest.json').read_text())
                if archive_root: manifest['products']['generation']['archiveRoot'] = archive_root
                self.publish(source, pointer, manifest)
                frozen, _ = freeze(project, pointer, binding, episodes)
                verify(frozen, pointer['sha256'], binding, episodes)

    def test_independent_verify_rejects_wrong_archive_semantics_even_with_rehashed_file_manifest(self):
        for failure in ('source', 'input', 'missing_instruction'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                project, source, pointer, binding, episodes, refs = self.archive_fixture(Path(temporary))
                frozen, _ = freeze(project, pointer, binding, episodes)
                detached = Path(temporary)/'detached'; shutil.move(frozen, detached)
                manifest = json.loads((detached/'manifest.json').read_text())
                if failure == 'missing_instruction': (detached/'archives'/refs[1]).unlink()
                else:
                    path = detached/'archives'/refs[0 if failure == 'source' else 2]
                    value = json.loads(path.read_bytes())
                    if failure == 'source': value['sourceFiles']['scene.py'] = '# wrong historical source\n'
                    else: value['text'] = 'wrong original input'
                    path.write_text(json.dumps(value, ensure_ascii=False))
                self.publish(detached, pointer, manifest)
                shutil.rmtree(project)
                with self.assertRaises(ValueError): verify(detached, pointer['sha256'], binding, episodes)

    def test_archive_references_reject_cross_project_escape_and_nonregular_originals(self):
        for failure in ('cross_project', 'escape', 'absolute', 'symlink', 'parent_symlink', 'directory', 'fifo'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                project, source, pointer, binding, episodes, refs = self.archive_fixture(Path(temporary))
                manifest = json.loads((source/'manifest.json').read_text())
                if failure in ('symlink', 'parent_symlink', 'directory', 'fifo'):
                    path = project/refs[0]
                    if failure == 'parent_symlink':
                        parent = path.parent; parent.rename(project/'real_sources'); parent.symlink_to(project/'real_sources', target_is_directory=True)
                    else:
                        original = Path(temporary)/'original'; original.write_bytes(path.read_bytes()); path.unlink()
                        if failure == 'symlink': path.symlink_to(original)
                        elif failure == 'directory': path.mkdir()
                        else: os.mkfifo(path)
                else:
                    rows = [json.loads(line) for line in (source/'raw.jsonl').read_text().splitlines()]
                    rows[0]['snapshotRef'] = {'cross_project':'projects/p_other/'+refs[0],
                        'escape':'../'+refs[0], 'absolute':'/'+refs[0]}[failure]
                    raw = b''.join(json.dumps(row).encode()+b'\n' for row in rows)
                    (source/'raw.jsonl').write_bytes(raw); (project/'generation-trace.jsonl').write_bytes(raw)
                    manifest['trace'] = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                self.publish(source, pointer, manifest)
                with self.assertRaises(ValueError): freeze(project, pointer, binding, episodes)

    def test_robot_only_freeze_does_not_require_generation_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, source, pointer, binding, episodes = self.fixture(Path(temporary))
            manifest = json.loads((source/'manifest.json').read_text())
            binding.update(requiredOutputs=['demonstrations'], environmentRefs=[dict(revision='rev_test', split='train')])
            episodes[0]['revision'] = 'rev_test'
            manifest.update(binding=binding, episodes=episodes)
            manifest.pop('trace'); manifest['products'].pop('generation'); manifest['products'].pop('environment')
            (source/'raw.jsonl').unlink(); (project/'generation-trace.jsonl').unlink()
            manifest['files'] = [row for row in manifest['files'] if row['path'] != 'raw.jsonl']
            data = json.dumps(manifest).encode(); (source/'manifest.json').write_bytes(data)
            pointer['sha256'] = hashlib.sha256(data).hexdigest()
            frozen, _ = freeze(project, pointer, binding, episodes)
            verify(frozen, pointer['sha256'], binding, episodes)

    def test_freezes_binary_closure_and_an_exact_trace_prefix_without_export_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, source, pointer, binding, episodes = self.fixture(Path(temporary))
            frozen, manifest = freeze(project, pointer, binding, episodes)
            self.assertEqual((frozen/'binary.bin').read_bytes(), (source/'binary.bin').read_bytes())
            (source/'binary.bin').write_bytes(b'producer edited after freeze')
            with (project/'generation-trace.jsonl').open('ab') as stream:
                stream.write(b'{"exported":true}\n')
            verify(frozen, pointer['sha256'], binding, episodes)
            self.assertEqual((frozen/'binary.bin').stat().st_size, 256*8192)
            self.assertEqual(manifest['binding'], binding)

    def test_wrong_source_selection_digest_and_trace_are_rejected(self):
        for failure in ('binding', 'selection', 'digest', 'trace'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                project, source, pointer, binding, episodes = self.fixture(Path(temporary))
                if failure == 'binding': binding = dict(binding, revision='other')
                if failure == 'selection': episodes = [dict(episodes[0], use='successful_demonstration')]
                if failure == 'digest': (source/'binary.bin').write_bytes(b'changed')
                if failure == 'trace': (project/'generation-trace.jsonl').write_bytes(b'other\n')
                with self.assertRaises(ValueError): freeze(project, pointer, binding, episodes)

    def test_retry_reuses_verified_frozen_bytes_without_allocating_a_second_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, source, pointer, binding, episodes, _ = self.archive_fixture(Path(temporary))
            frozen, manifest = freeze(project, pointer, binding, episodes)
            (source/'binary.bin').unlink()
            with patch.object(read_delivery.tempfile, 'mkdtemp', side_effect=AssertionError('duplicate freeze copy')):
                self.assertEqual(freeze(project, pointer, binding, episodes), (frozen, manifest))

    def test_retry_rechecks_frozen_closure_binding_original_trace_and_archives(self):
        for failure in ('frozen', 'binding', 'episodes', 'trace', 'archive'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                project, source, pointer, binding, episodes, refs = self.archive_fixture(Path(temporary))
                frozen, _ = freeze(project, pointer, binding, episodes)
                if failure == 'frozen': (frozen/'binary.bin').write_bytes(b'changed')
                elif failure == 'binding': binding = dict(binding, revision='other')
                elif failure == 'episodes': episodes = []
                elif failure == 'trace': (project/'generation-trace.jsonl').write_bytes(b'changed\n')
                else: (project/refs[0]).write_bytes(b'changed original archive')
                with self.assertRaises(ValueError): freeze(project, pointer, binding, episodes)

    def test_symlink_escape_and_modified_frozen_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, source, pointer, binding, episodes = self.fixture(Path(temporary))
            original = (source/'binary.bin').read_bytes()
            outside = Path(temporary)/'outside'; outside.write_bytes(original)
            (source/'binary.bin').unlink(); (source/'binary.bin').symlink_to(outside)
            with self.assertRaises(ValueError): freeze(project, pointer, binding, episodes)
            (source/'binary.bin').unlink(); (source/'binary.bin').write_bytes(original)
            frozen, _ = freeze(project, pointer, binding, episodes)
            (frozen/'binary.bin').write_bytes(b'tampered')
            with self.assertRaises(ValueError): verify(frozen, pointer['sha256'], binding, episodes)

    def test_file_declarations_cannot_escape_or_alias_the_manifest(self):
        for name in ('../outside', '/absolute', 'nested/../file', 'nested\\file', 'manifest.json', 'binary.bin'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                project, source, pointer, binding, episodes = self.fixture(Path(temporary))
                manifest = json.loads((source/'manifest.json').read_text())
                manifest['files'].append(dict(path=name, bytes=0, sha256='0'*64))
                data = json.dumps(manifest).encode(); (source/'manifest.json').write_bytes(data)
                pointer['sha256'] = hashlib.sha256(data).hexdigest()
                with self.assertRaises(ValueError): freeze(project, pointer, binding, episodes)

    def test_checked_file_or_parent_cannot_be_swapped_to_a_symlink_before_open(self):
        for replace_parent in (False, True):
            with self.subTest(parent=replace_parent), tempfile.TemporaryDirectory() as temporary:
                project, source, pointer, binding, episodes = self.fixture(Path(temporary))
                manifest = json.loads((source/'manifest.json').read_text())
                manifest['files'].sort(key=lambda row: row['path'] == 'binary.bin')
                data = json.dumps(manifest).encode(); (source/'manifest.json').write_bytes(data)
                pointer['sha256'] = hashlib.sha256(data).hexdigest()
                outside = Path(temporary)/'outside'; outside.mkdir()
                for file in source.iterdir(): (outside/file.name).write_bytes(file.read_bytes())
                original = read_delivery.plain; swapped = False
                def race(root, name):
                    nonlocal swapped
                    path = original(root, name)
                    if path == source/'binary.bin' and not swapped:
                        swapped = True
                        if replace_parent:
                            source.rename(source.with_name('original'))
                            source.symlink_to(outside, target_is_directory=True)
                        else:
                            path.unlink(); path.symlink_to(outside/'binary.bin')
                    return path
                with patch.object(read_delivery, 'plain', side_effect=race):
                    with self.assertRaises(ValueError): freeze(project, pointer, binding, episodes)

    def test_copy_storage_errors_are_not_misreported_as_invalid_source_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, source, _, _, _ = self.fixture(Path(temporary))
            failure = OSError(28, 'SYSTEM_VERIFICATION_ONLY no space left on destination')
            with self.assertRaises(OSError) as caught:
                with read_delivery.open_plain(project, 'delivery/run/binary.bin'):
                    raise failure
            self.assertIs(caught.exception, failure)


@unittest.skipUnless(os.environ.get('SCENEAGENT_OFFICIAL_DELIVERY_FIXTURE'), 'requires the existing locked CPU official-reader fixture')
class OfficialDeliveryArtifactsTests(unittest.TestCase):
    """Synthetic format fixtures only: no robot execution or native environment claim."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)/'projects/p_format_fixture'
        self.source = self.project/'delivery/run'
        shutil.copytree(os.environ['SCENEAGENT_OFFICIAL_DELIVERY_FIXTURE'], self.source)
        self.manifest = json.loads((self.source/'manifest.json').read_text())
        for report in self.source.glob('read-*.json'): report.unlink()
        generated = self.manifest['products']['generation']
        (self.project/'generation-trace.jsonl').write_bytes((self.source/generated['rawTrace']).read_bytes()+b'{"later":true}\n')
        self.evaluator = (self.source/self.manifest['products']['environment']['entrypoint']).read_text()
        self.consumer = hashlib.sha256(Path(read_delivery.__file__).read_bytes()).hexdigest()

    def selected(self, outputs):
        refs = [dict(revision='format_fixture', split='train'), dict(revision='format_fixture_other', split='test')]
        self.manifest['binding'].update(requiredOutputs=outputs, environmentRefs=refs)
        self.manifest['episodes'] = [dict(row, revision=refs[i]['revision']) for i, row in enumerate(self.manifest['episodes'])]
        robot = self.manifest['products']['robot'][0]
        mapping_path = self.source/robot['mapping']
        mapping = json.loads(mapping_path.read_text())
        for row, selected in zip(mapping['episodes'], self.manifest['episodes']): row['revision'] = selected['revision']
        mapping_path.write_text(json.dumps(mapping))
        environment = self.manifest['products']['environment']
        build = self.source/environment['root']/'projects/p_format_fixture/revisions'
        shutil.copytree(build/'format_fixture', build/'format_fixture_other')
        if 'generation' not in outputs:
            for name in self.manifest['products'].pop('generation').values(): (self.source/name).unlink()
            self.manifest.pop('trace')
        if 'demonstrations' not in outputs:
            self.manifest['episodes'] = []
            for group in self.manifest['products'].pop('robot'):
                for name in group.values():
                    target = self.source/name
                    if target.is_dir(): shutil.rmtree(target)
                    else: target.unlink()
        if 'evaluation' not in outputs:
            environment = self.manifest['products'].pop('environment')
            shutil.rmtree(self.source/environment['root'])
            for key in ('entrypoint', 'readme', 'runtime'): (self.source/environment[key]).unlink()

    def publish(self):
        self.manifest['files'] = [dict(path=p.relative_to(self.source).as_posix(), bytes=p.stat().st_size,
            sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(self.source.rglob('*'))
            if p.is_file() and p.relative_to(self.source).as_posix() != 'manifest.json']
        data = json.dumps(self.manifest).encode(); (self.source/'manifest.json').write_bytes(data)
        return dict(manifestRef='projects/p_format_fixture/delivery/run/manifest.json', sha256=hashlib.sha256(data).hexdigest())

    def consume(self):
        pointer = self.publish()
        result = read_delivery.consume(self.project, pointer, self.manifest['binding'], self.manifest['episodes'], self.consumer, self.evaluator)
        resumed = read_delivery.consume(self.project, pointer, self.manifest['binding'], self.manifest['episodes'], self.consumer,
            self.evaluator, frozen_ref=result['frozenManifestRef'])
        self.assertEqual(resumed, result)
        return result

    def test_scene_scope_reads_only_generation_and_both_environment_revisions(self):
        self.selected(['generation', 'evaluation'])
        result = self.consume()
        self.assertEqual(set(result['checks']), {'libraries', 'generation', 'environment'})
        self.assertEqual(set(result['checks']['libraries']), {'harbor'})
        self.assertEqual(set(result['products']), {'generation', 'environment'})
        self.assertEqual(result['episodes'], [])
        self.assertEqual(result['checks']['generation']['trainingRows'], 1)
        self.assertTrue(result['environmentRoot'].endswith('/environment/data'))

    def test_robot_scope_reads_both_formats_and_keeps_each_episode_revision(self):
        self.selected(['demonstrations'])
        result = self.consume()
        self.assertEqual(set(result['checks']), {'libraries', 'robot', 'robomimicReaderSha256'})
        self.assertEqual(set(result['products']), {'robot'})
        self.assertNotIn('environmentRoot', result)
        self.assertEqual([row['revision'] for row in result['episodes']], ['format_fixture', 'format_fixture_other'])
        self.assertEqual(result['checks']['robot'][0]['samples'], 7)

    def test_full_selected_scope_reads_all_formats_and_preserves_legacy_full_scope(self):
        legacy = self.consume()
        self.assertEqual(legacy['checks']['robot'][0]['samples'], 7)
        self.selected(['generation', 'demonstrations', 'evaluation'])
        result = self.consume()
        self.assertEqual(set(result['products']), {'generation', 'robot', 'environment'})
        self.assertEqual([row['revision'] for row in result['episodes']], ['format_fixture', 'format_fixture_other'])

    def test_robot_mapping_cannot_relabel_the_selected_episode_revision(self):
        self.selected(['demonstrations'])
        mapping_path = self.source/self.manifest['products']['robot'][0]['mapping']
        mapping = json.loads(mapping_path.read_text()); mapping['episodes'][1]['revision'] = 'format_fixture'
        mapping_path.write_text(json.dumps(mapping))
        with self.assertRaisesRegex(ValueError, 'selected source episodes'):
            self.consume()

    def test_environment_scope_requires_every_selected_revision_in_the_same_project_package(self):
        self.selected(['generation', 'evaluation'])
        root = self.source/self.manifest['products']['environment']['root']
        shutil.rmtree(root/'projects/p_format_fixture/revisions/format_fixture_other')
        with self.assertRaisesRegex(ValueError, 'absent from closure'):
            self.consume()

    def test_missing_robot_format_and_changed_selected_trace_still_fail(self):
        self.selected(['generation', 'demonstrations', 'evaluation'])
        (self.source/self.manifest['products']['robot'][0]['hdf5']).unlink()
        with self.assertRaisesRegex(ValueError, 'absent from closure'):
            self.consume()
        (self.project/'generation-trace.jsonl').write_bytes(b'changed\n')
        with self.assertRaisesRegex(ValueError, 'trace'):
            self.consume()


if __name__ == '__main__': unittest.main()
