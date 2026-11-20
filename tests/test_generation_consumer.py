import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from examples.read_generation import consume


class GenerationConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)/'projects'/'p_test'
        (self.project/'.trace'/'sources').mkdir(parents=True)
        self.events = []

    def event(self, type, **fields):
        if type == 'tool_call':
            fields.setdefault('actor', 'codex-mcp')
        event = dict(type=type, eventId=f'p_test:{len(self.events)+1}', generationId='g_test', requirementVersion=1, **fields)
        self.events.append(event)
        return event

    def snapshot(self, source, previous=None):
        files = {'scene.py': source, 'tasks.py': 'def build(world): return []\n', 'expert.py': 'def run(env, task): pass\n'}
        digest = hashlib.sha256(json.dumps(files, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        relative = f'.trace/sources/{digest}.json'
        (self.project/relative).write_text(json.dumps(dict(sourceHash=digest, sourceFiles=files), ensure_ascii=False))
        event = self.event('source_snapshot', sourceHash=digest, previousSourceHash=previous, snapshotRef=relative)
        return event

    def write(self):
        (self.project/'generation-trace.jsonl').write_text(''.join(json.dumps(event, ensure_ascii=False)+'\n' for event in self.events))

    def test_actual_tool_parameters_and_return_are_consumed_with_unknown_context_explicit(self):
        self.event('user_message', message='生成中文实验室。')
        call = self.event('tool_call', name='assets_lookup', args={'query': '工作台'}, requestId='lookup-1')
        self.event('tool_result', name='assets_lookup', callEventId=call['eventId'], result={'items': [{'assetId': 'worktable-1'}]})
        self.write()
        report = consume(self.project, 'g_test')
        self.assertTrue(report['passed'])
        self.assertEqual(report['toolSampleCount'], 1)
        sample = self.restored(report)[0]
        self.assertEqual(sample['action']['args'], {'query': '工作台'})
        self.assertEqual(sample['returned']['items'][0]['assetId'], 'worktable-1')
        self.assertEqual(sample['actor'], 'codex-mcp')
        self.assertTrue(sample['agentTrainingEligible'])
        self.assertEqual(report['coverage']['internalContext'], 'unknown')

    def test_original_input_hash_and_transitive_instructions_are_checked_before_consume_and_restore(self):
        for failure in ('digest', 'declaration', 'instructions'):
            with self.subTest(failure=failure):
                self.events = []
                value = dict(kind='native_turn_input', text='中文与空白  \n',
                             instructionsRef='.trace/inputs/'+'c'*64+'.json' if failure == 'instructions' else None)
                data = ('\n'+json.dumps(value, ensure_ascii=False, indent=2)+' \n').encode()
                sha = hashlib.sha256(data).hexdigest(); ref = '.trace/inputs/'+sha+'.json'
                path = self.project/ref; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
                self.event('input_snapshot', snapshotRef=ref, inputHash='0'*64 if failure == 'declaration' else sha)
                call = self.event('tool_call', name='assets_lookup', args={})
                self.event('tool_result', name='assets_lookup', callEventId=call['eventId'], result={})
                self.write()
                if failure == 'digest':
                    good = consume(self.project, 'g_test')
                    self.assertTrue(good['passed'])
                    path.write_bytes(data+b' ')
                    with self.assertRaises(ValueError): self.restored(good)
                self.assertFalse(consume(self.project, 'g_test')['passed'])

    def test_archive_references_from_prior_generation_are_checked_without_parsing_text(self):
        sha = 'b'*64
        self.event('input_snapshot', snapshotRef='.trace/inputs/'+sha+'.json', inputHash=sha)['generationId'] = 'g_old'
        call = self.event('tool_call', name='assets_lookup', args={'query': 'text .trace/inputs/'+sha+'.json'})
        self.event('tool_result', name='assets_lookup', callEventId=call['eventId'], result={'instructionsRef': 'ordinary-guide.md'})
        self.write()
        self.assertFalse(consume(self.project, 'g_test')['passed'])
        self.events.pop(0)
        self.write()
        report = consume(self.project, 'g_test')
        self.assertTrue(report['passed'])
        self.assertEqual(report['coverage']['archiveClosure'], dict(scope='raw_prefix_source_input_archives',
            complete=True, archiveCount=0, sourceCount=0, inputCount=0))

    def test_archive_qualified_project_refs_preserve_chinese_whitespace_and_reject_links(self):
        instructions = ('\n'+json.dumps(dict(kind='thread_instructions', text='真实中文  \n'), ensure_ascii=False, indent=2)+' \n').encode()
        sha = hashlib.sha256(instructions).hexdigest(); ref = '.trace/inputs/'+sha+'.json'
        path = self.project/ref; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(instructions)
        data = json.dumps(dict(kind='native_turn_input', instructionsRef='projects/p_test/'+ref), ensure_ascii=False).encode()
        input_hash = hashlib.sha256(data).hexdigest(); input_ref = '.trace/inputs/'+input_hash+'.json'
        (self.project/input_ref).write_bytes(data)
        self.event('input_snapshot', snapshotRef='projects/p_test/'+input_ref, inputHash=input_hash)
        call = self.event('tool_call', name='assets_lookup', args={})
        self.event('tool_result', name='assets_lookup', callEventId=call['eventId'], result={})
        self.write(); report = consume(self.project, 'g_test')
        self.assertTrue(report['passed']); self.assertEqual(report['coverage']['archiveClosure']['inputCount'], 2)
        self.restored(report)
        outside = Path(self.temp.name)/'original'; outside.write_bytes(instructions)
        path.unlink(); path.symlink_to(outside)
        self.assertFalse(consume(self.project, 'g_test')['passed'])
        with self.assertRaises(ValueError): self.restored(report)

    def test_named_sdk_array_reference_is_preserved_and_rechecked_without_claiming_physics(self):
        # SYSTEM_VERIFICATION_ONLY: this reader verifies trace bytes, not an SDK measurement.
        import numpy as np
        target=self.project/'episodes/e_test/arrays/points.npy';target.parent.mkdir(parents=True)
        np.save(target,np.arange(9,dtype=np.float32).reshape(3,3),allow_pickle=False)
        reference={'ref':str(target.relative_to(self.project)),
                   'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'dtype':'float32','shape':[3,3]}
        call=self.event('tool_call',name='robot_run',args={'taskId':'generic'},requestId='run-array')
        self.event('tool_result',name='robot_run',callEventId=call['eventId'],result={'stream':reference})
        self.write();report=consume(self.project,'g_test')
        self.assertTrue(report['passed'])
        self.assertEqual(self.restored(report)[0]['returned']['stream'],reference)
        target.write_bytes(target.read_bytes()[:-1]+b'X')
        self.assertFalse(consume(self.project,'g_test')['passed'])
        with self.assertRaisesRegex(ValueError,'hash mismatch'):self.restored(report)

    def array_package(self, relative, revision='r_build', episode=None, offset=0):
        # SYSTEM_VERIFICATION_ONLY: use the production writer, not a prefixed fake reference.
        import numpy as np
        from runtime.records import _array_reference
        directory = self.project/relative
        directory.mkdir(parents=True, exist_ok=True)
        reference = _array_reference(directory, np.arange(168*3, dtype=np.float32).reshape(168, 3)+offset)
        self.assertEqual(set(reference), {'ref', 'sha256', 'dtype', 'shape'})
        self.assertEqual(reference['ref'], 'arrays/'+reference['sha256']+'.npy')
        initial = dict(time=0., simulationTime=.01, timeOrigin=.01, physicsStep=0, stageId=1,
                       streams={'particles': reference}, streamStatus={'particles': {'status': 'available'}})
        result = dict(schema='sceneagent-episode-v6' if episode else 'sceneagent-build-v6',
                      projectId='p_test', revision=revision,
                      artifacts={'manifest': 'projects/p_test/'+relative+'/manifest.json'}, initialState=initial)
        if episode:
            result['episodeId'] = episode
        else:
            result['compiled'] = {'initialState': initial}
        (directory/'manifest.json').write_text(json.dumps(result))
        return result, reference, directory/reference['ref']

    def array_report(self, returned):
        self.events = []
        call = self.event('tool_call', name='scene_build', args={'expectedSourceHash': 'frozen'}, requestId='array-build')
        self.event('tool_result', name='scene_build', callEventId=call['eventId'], result=returned)
        return self.frozen_report()

    def test_production_build_array_uses_its_revision_scope_in_consume_and_restore(self):
        result, reference, target = self.array_package('revisions/r_build')
        report = self.array_report(result)
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(self.restored(report)[0]['returned'], result)
        self.assertEqual(result['initialState']['streams']['particles'], reference)
        target.write_bytes(target.read_bytes()[:-1]+b'X')
        self.assertFalse(consume(self.project, 'g_test')['passed'])
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.restored(report)

    def test_nested_job_and_mcp_children_keep_independent_revision_scopes(self):
        first, unused, unused_path = self.array_package('revisions/r_first', revision='r_first')
        second, unused, second_path = self.array_package('revisions/r_second', revision='r_second', offset=1)
        first['childBuild'] = second
        text = json.dumps(second)
        returned = {'status': 'completed', 'result': first, 'events': [{'result': second}],
                    'mcp': {'content': [{'type': 'text', 'text': text}], 'structuredContent': second}}
        report = self.array_report(returned)
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(self.restored(report)[0]['returned'], returned)
        self.assertEqual(self.restored(report)[0]['returned']['mcp']['content'][0]['text'], text)
        second_path.unlink()
        self.assertFalse(consume(self.project, 'g_test')['passed'])
        with self.assertRaises((ValueError, OSError)):
            self.restored(report)

    def test_episode_and_evaluation_array_scopes_override_parent_revision(self):
        build, unused, unused_path = self.array_package('revisions/r_build')
        episode, unused, unused_path = self.array_package('episodes/e_run', episode='e_run', offset=1)
        evaluation, unused, unused_path = self.array_package('evaluations/check_1/episodes/e_eval', episode='e_eval', offset=2)
        build['episodes'] = [episode, evaluation]
        report = self.array_report(build)
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(self.restored(report)[0]['returned'], build)

    def test_local_array_cannot_guess_a_root_or_search_another_package(self):
        result, reference, target = self.array_package('revisions/r_other', revision='r_other')
        # A same-SHA project-root copy must not legitimize an ownerless local reference.
        (self.project/'arrays').mkdir()
        (self.project/reference['ref']).write_bytes(target.read_bytes())
        wrong = dict(result, revision='r_missing', artifacts={'manifest': 'projects/p_test/revisions/r_missing/manifest.json'})
        (self.project/'revisions/r_missing').mkdir()
        (self.project/'revisions/r_missing/manifest.json').write_text(json.dumps(wrong))
        for returned in ({'stream': reference}, wrong):
            with self.subTest(owner=returned.get('revision')):
                report = self.array_report(returned)
                self.assertFalse(report['passed'])
                with self.assertRaises((ValueError, OSError)):
                    self.restored(report)

    def test_conflicting_owner_and_sibling_do_not_borrow_a_valid_array_scope(self):
        import copy
        result, reference, unused = self.array_package('revisions/r_build')
        conflict = copy.deepcopy(result)
        conflict['child'] = dict(result, revision='r_other')
        cross_project = copy.deepcopy(result)
        cross_project['child'] = dict(result, projectId='p_other')
        sibling = {'completed': result, 'unowned': {'streams': {'particles': reference}}}
        for returned in (conflict, cross_project, sibling):
            with self.subTest(returned=returned.get('projectId', 'siblings')):
                report = self.array_report(returned)
                self.assertFalse(report['passed'])
                with self.assertRaises((ValueError, OSError)):
                    self.restored(report)

    def test_partial_child_identity_cannot_inherit_parent_but_complete_episode_can_override(self):
        import copy
        result, reference, target = self.array_package('revisions/r_build')
        for ref in (reference['ref'], 'projects/p_test/'+str(target.relative_to(self.project))):
            bad = copy.deepcopy(result)
            bad['child'] = {'projectId': 'p_test', 'revision': 'r_child',
                            'stream': dict(reference, ref=ref)}
            with self.subTest(ref=ref):
                report = self.array_report(bad)
                self.assertFalse(report['passed'])
                with self.assertRaises((ValueError, OSError)):
                    self.restored(report)
        episode, unused, unused_path = self.array_package('evaluations/check_1/episodes/e_eval', episode='e_eval')
        wrapper = {'projectId': 'p_test', 'revision': 'r_build', 'evaluationId': 'check_1',
                   'container': {'episodes': [episode]}}
        report = self.array_report(wrapper)
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(self.restored(report)[0]['returned'], wrapper)

    def test_known_array_owner_rejects_escape_explicit_other_package_and_symlink(self):
        import copy
        result, reference, target = self.array_package('revisions/r_build')
        other, unused, other_target = self.array_package('revisions/r_other', revision='r_other')
        for ref in ('../r_other/'+reference['ref'],
                    'projects/p_test/revisions/r_other/'+reference['ref'],
                    'projects/p_other/revisions/r_build/'+reference['ref']):
            bad = copy.deepcopy(result)
            bad['initialState']['streams']['particles']['ref'] = ref
            with self.subTest(ref=ref):
                report = self.array_report(bad)
                self.assertFalse(report['passed'])
                with self.assertRaises((ValueError, OSError)):
                    self.restored(report)
        target.unlink()
        target.symlink_to(other_target)
        report = self.array_report(result)
        self.assertFalse(report['passed'])
        with self.assertRaises((ValueError, OSError)):
            self.restored(report)

    def test_scoped_array_does_not_change_project_image_or_legacy_explicit_array_contract(self):
        import base64
        result, reference, target = self.array_package('revisions/r_build')
        data = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a9sAAAAASUVORK5CYII=')
        image = self.project/'outside-build.png'
        image.write_bytes(data)
        result['image'] = {'type': 'image', 'mimeType': 'image/png', 'ref': image.name,
                           'sha256': hashlib.sha256(data).hexdigest()}
        report = self.array_report(result)
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(self.restored(report)[0]['returned'], result)
        explicit = dict(reference, ref='projects/p_test/'+str(target.relative_to(self.project)))
        report = self.array_report({'stream': explicit})
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(self.restored(report)[0]['returned']['stream'], explicit)

    def test_edit_restores_both_source_versions_and_links_failed_build_then_repair(self):
        self.event('codex', method='transport/sent', params={'message': {'method': 'turn/start', 'params': {
            'threadId': 'thread-1', 'input': [{'type': 'text', 'text': '生成实验室并修复。'}]}}})
        before = self.snapshot('def build(world): pass\n')
        self.event('codex', method='item/completed', params={'item': {'id': 'edit-failed', 'type': 'fileChange'}})
        failed = self.snapshot('def build(world): raise ValueError("失败")\n', before['sourceHash'])
        failed['cause'] = 'edit-failed'
        call = self.event('tool_call', name='scene_build', args={'expectedSourceHash': failed['sourceHash']}, requestId='build-fail')
        self.event('tool_result', name='scene_build', callEventId=call['eventId'], result={'jobId': 'job-fail', 'status': 'pending'})
        self.event('operation_failed', jobId='job-fail', requestId='build-fail', error='失败')
        native = self.event('codex', method='item/completed', params={'item': {'id': 'edit-repaired', 'type': 'commandExecution'}})
        repaired = self.snapshot('def build(world): world.space("实验室")\n', failed['sourceHash'])
        repaired['cause'] = 'edit-repaired'
        call = self.event('tool_call', name='scene_build', args={'expectedSourceHash': repaired['sourceHash']}, requestId='build-ok')
        self.event('tool_result', name='scene_build', callEventId=call['eventId'], result={'jobId': 'job-ok', 'status': 'pending'})
        self.event('operation_completed', jobId='job-ok', requestId='build-ok', result={'revision': 'rev-fixed'})
        self.write()
        report = consume(self.project, 'g_test')
        self.assertTrue(report['passed'])
        edits = [sample for sample in self.restored(report) if sample['kind'] == 'edit']
        self.assertEqual(len(edits), 2)
        self.assertEqual(edits[1]['beforeSources']['scene.py'], 'def build(world): raise ValueError("失败")\n')
        self.assertEqual(edits[1]['afterSources']['scene.py'], 'def build(world): world.space("实验室")\n')
        self.assertIn('operation_failed', [outcome['type'] for outcome in edits[0]['outcomes']])
        self.assertIn('rev-fixed', json.dumps(edits[1]['outcomes']))
        self.assertIn('scene.py', edits[1]['diffs'])
        self.assertTrue(edits[1]['agentTrainingEligible'])
        self.assertEqual(edits[1]['actor'], 'codex-native')
        self.assertEqual(edits[1]['nativeActionRef'], native['eventId'])

    def test_only_manual_verification_or_unknown_tools_cannot_pass_as_agent_training(self):
        for actor in ('manual', 'verification', None):
            self.events = []
            call = self.event('tool_call', name='scene_build', actor=actor, args={'expectedSourceHash': 'known'}, requestId='build-1')
            self.event('tool_result', name='scene_build', callEventId=call['eventId'], result={'jobId': 'job-1'})
            self.event('operation_completed', jobId='job-1', result={'revision': 'rev-1'})
            self.write()
            report = consume(self.project)
            self.assertFalse(report['passed'])
            self.assertEqual(report['agentTrainingSampleCount'], 0)
            self.assertEqual(report['diagnosticSampleCount'], 1)
            self.assertFalse(self.restored(report)[0]['agentTrainingEligible'])
            self.assertIn('rev-1', json.dumps(self.restored(report)[0]['outcomes']))

    def test_legacy_call_without_actor_remains_an_unknown_diagnostic(self):
        call = self.event('tool_call', name='project_inspect', args={})
        del call['actor']
        self.event('tool_result', name='project_inspect', callEventId=call['eventId'], result={'revision': 'rev-1'})
        self.write()
        report = consume(self.project)
        self.assertFalse(report['passed'])
        self.assertEqual(self.restored(report)[0]['actor'], 'unknown')
        self.assertEqual(report['coverage']['internalContext'], 'unknown')

    def test_visible_user_reference_without_actual_native_input_does_not_qualify_an_edit(self):
        user = self.event('user_message', message='只提交但尚未实际发送的输入。')
        before = self.snapshot('def build(world): pass\n')
        after = self.snapshot('def build(world): world.space("workspace")\n', before['sourceHash'])
        after['visibleInputRefs'] = [user['eventId']]
        call = self.event('tool_call', actor='verification', name='scene_build', args={'expectedSourceHash': after['sourceHash']}, requestId='build-1')
        self.event('tool_result', name='scene_build', callEventId=call['eventId'], result={'revision': 'rev-1'})
        self.write()
        report = consume(self.project)
        self.assertFalse(report['passed'])
        self.assertEqual(report['agentTrainingSampleCount'], 0)

    def test_earlier_native_input_does_not_prove_unknown_edit_authorship(self):
        self.event('codex', method='transport/sent', params={'message': {'method': 'turn/start', 'params': {
            'threadId': 'thread-1', 'input': [{'type': 'text', 'text': '生成工作区。'}]}}})
        before = self.snapshot('def build(world): pass\n')
        after = self.snapshot('def build(world): world.space("workspace")\n', before['sourceHash'])
        call = self.event('tool_call', actor='verification', name='scene_build',
                          args={'expectedSourceHash': after['sourceHash']}, requestId='build-1')
        self.event('tool_result', name='scene_build', callEventId=call['eventId'], result={'revision': 'rev-1'})
        self.write()
        report = consume(self.project)
        edit = next(sample for sample in self.restored(report) if sample['kind'] == 'edit')
        self.assertEqual(edit['actor'], 'unknown')
        self.assertEqual(edit['actorProvenance'], 'source_author_unknown')
        self.assertFalse(edit['agentTrainingEligible'])
        self.assertFalse(report['passed'])
        self.assertEqual(report['agentTrainingSampleCount'], 0)
        self.assertEqual(edit['afterSources']['scene.py'], 'def build(world): world.space("workspace")\n')
        self.assertIn('rev-1', json.dumps(edit['outcomes']))
        after['actor'] = 'codex-native'
        self.write()
        claimed = consume(self.project)
        self.assertFalse(next(sample for sample in self.restored(claimed) if sample['kind'] == 'edit')['agentTrainingEligible'])

    def test_agent_message_completion_is_not_source_edit_evidence(self):
        self.event('codex', method='transport/sent', params={'message': {'method': 'turn/start', 'params': {
            'input': [{'type': 'text', 'text': '生成工作区。'}]}}})
        before = self.snapshot('def build(world): pass\n')
        self.event('codex', method='item/completed', params={'item': {'id': 'message-1', 'type': 'agentMessage', 'text': '完成。'}})
        after = self.snapshot('def build(world): world.space("workspace")\n', before['sourceHash'])
        after['cause'] = 'message-1'
        call = self.event('tool_call', actor='verification', name='scene_build',
                          args={'expectedSourceHash': after['sourceHash']}, requestId='build-1')
        self.event('tool_result', name='scene_build', callEventId=call['eventId'], result={'revision': 'rev-1'})
        self.write()
        report = consume(self.project)
        edit = next(sample for sample in self.restored(report) if sample['kind'] == 'edit')
        self.assertFalse(edit['agentTrainingEligible'])
        self.assertEqual(edit['actorProvenance'], 'source_author_unknown')

    def test_missing_or_corrupted_source_cannot_pass_despite_a_valid_tool_sample(self):
        snapshot = self.snapshot('def build(world): pass\n')
        call = self.event('tool_call', name='project_inspect', args={})
        self.event('tool_result', name='project_inspect', callEventId=call['eventId'], result={'revision': 'rev-1'})
        self.write()
        path = self.project/snapshot['snapshotRef']
        path.unlink()
        self.assertFalse(consume(self.project)['passed'])
        path.write_text(json.dumps({'sourceHash': snapshot['sourceHash'], 'sourceFiles': {'scene.py': 'tampered'}}))
        report = consume(self.project)
        self.assertFalse(report['passed'])
        self.assertIn('hash', json.dumps(report['issues']).lower())

    def test_missing_image_and_unpaired_tool_result_are_rejected(self):
        call = self.event('tool_call', name='scene_observe', args={'revision': 'rev-1'})
        self.event('tool_result', name='scene_observe', callEventId=call['eventId'], result={'imageRef': 'missing.png'})
        self.write()
        self.assertFalse(consume(self.project)['passed'])
        self.events[-1]['callEventId'] = 'missing-call'
        self.events[-1]['result'] = {'state': {}}
        self.write()
        report = consume(self.project)
        self.assertFalse(report['passed'])
        self.assertEqual(report['sampleCount'], 0)

    def test_empty_trace_and_duplicate_event_ids_do_not_pass(self):
        self.write()
        self.assertFalse(consume(self.project)['passed'])
        call = self.event('tool_call', name='assets_lookup', args={'query': 'cup'})
        result = self.event('tool_result', name='assets_lookup', callEventId=call['eventId'], result={'items': []})
        result['eventId'] = call['eventId']
        self.write()
        self.assertFalse(consume(self.project)['passed'])

    def test_project_reference_cannot_escape_to_another_project(self):
        call = self.event('tool_call', name='scene_observe', args={'revision': 'rev-1'})
        self.event('tool_result', name='scene_observe', callEventId=call['eventId'], result={'imageRef': '../other/private.png'})
        self.write()
        report = consume(self.project)
        self.assertFalse(report['passed'])
        self.assertIn('scope', json.dumps(report['issues']).lower())

    def test_archived_native_image_input_and_mcp_image_output_remain_consumable(self):
        image_data = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a9sAAAAASUVORK5CYII='
        self.event('codex', method='transport/sent', params={'message': {'method': 'turn/start', 'params': {
            'input': [{'type': 'image', 'url': f'data:image/png;base64,{image_data}'}]}}})
        call = self.event('tool_call', name='scene_observe', args={'revision': 'rev-1'})
        self.event('tool_result', name='scene_observe', callEventId=call['eventId'], result={
            'content': [{'type': 'image', 'data': image_data, 'mimeType': 'image/png'}]})
        self.write()
        self.assertTrue(consume(self.project)['passed'])
        self.events[0]['params']['message']['params']['input'][0]['url'] = 'https://example.com/unarchived.png'
        self.write()
        self.assertFalse(consume(self.project)['passed'])

    def test_explicit_failed_tool_receipt_is_useful_repair_data(self):
        failed = self.event('tool_call', name='scene_build', args={'expectedSourceHash': 'bad'}, requestId='build-fail')
        self.event('tool_result', name='scene_build', callEventId=failed['eventId'], error='真实构建失败')
        repaired = self.event('tool_call', name='scene_build', args={'expectedSourceHash': 'fixed'}, requestId='build-ok')
        self.event('tool_result', name='scene_build', callEventId=repaired['eventId'], result={'revision': 'rev-fixed'})
        self.write()
        report = consume(self.project)
        self.assertTrue(report['passed'])
        self.assertEqual(report['toolSampleCount'], 2)
        self.assertTrue(self.restored(report)[0]['returned']['isError'])
        self.assertEqual(self.restored(report)[0]['returned']['returnedError'], '真实构建失败')
        self.events[1]['callEventId'] = 'nonexistent-call'
        self.write()
        self.assertFalse(consume(self.project)['passed'])

    def test_async_job_image_descriptor_checks_bytes_and_declared_digest(self):
        image_data = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a9sAAAAASUVORK5CYII='
        import base64
        digest = hashlib.sha256(base64.b64decode(image_data)).hexdigest()
        image_path = self.project/'revisions'/'rev-1'/'view.png'
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(base64.b64decode(image_data))
        call = self.event('tool_call', name='job', args={'jobId': 'job-observe', 'action': 'wait'})
        returned = self.event('tool_result', name='job', callEventId=call['eventId'], result={'status': 'completed',
            'artifactRef': 'projects/p_test/revisions/rev-1/view.png',
            'image': {'data': image_data, 'mimeType': 'image/png', 'sha256': digest}})
        self.write()
        self.assertTrue(consume(self.project)['passed'])
        image_path.write_bytes(b'wrong frame')
        self.assertFalse(consume(self.project)['passed'])
        image_path.write_bytes(base64.b64decode(image_data))
        returned['result']['image']['sha256'] = '0'*64
        self.write()
        self.assertFalse(consume(self.project)['passed'])
        returned['result']['image']['data'] = 'not valid base64'
        self.write()
        self.assertFalse(consume(self.project)['passed'])

    def test_explicit_frozen_trace_is_consumed_after_live_trace_changes(self):
        call = self.event('tool_call', name='assets_lookup', args={'query': 'worktable'})
        self.event('tool_result', name='assets_lookup', callEventId=call['eventId'], result={'items': [{'assetId': 'table-1'}]})
        self.write()
        frozen = self.project/'.trace'/'traces'/'original.jsonl'
        frozen.parent.mkdir(parents=True)
        frozen.write_bytes((self.project/'generation-trace.jsonl').read_bytes())
        expected_hash = hashlib.sha256(frozen.read_bytes()).hexdigest()
        self.events = []
        self.write()
        self.assertFalse(consume(self.project)['passed'])
        report = consume(self.project, 'g_test', trace_ref='.trace/traces/original.jsonl')
        self.assertTrue(report['passed'])
        self.assertEqual(report['toolSampleCount'], 1)
        self.assertEqual(self.restored(report)[0]['returned']['items'][0]['assetId'], 'table-1')
        self.assertEqual(report['traceRef'], '.trace/traces/original.jsonl')
        self.assertEqual(report['traceHash'], expected_hash)

    def test_asset_report_and_thumbnail_are_distinct_references_and_both_are_checked(self):
        import base64
        image_data = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a9sAAAAASUVORK5CYII='
        directory = self.project/'asset-previews'/'prepared-1'
        directory.mkdir(parents=True)
        report_path, thumbnail_path = directory/'report.json', directory/'thumbnail.png'
        report_path.write_text(json.dumps({'assetId': 'chair-1'}))
        thumbnail_path.write_bytes(base64.b64decode(image_data))
        call = self.event('tool_call', name='assets_prepare', args={'assetId': 'chair-1'})
        returned = self.event('tool_result', name='assets_prepare', callEventId=call['eventId'], result={
            'artifactRef': 'projects/p_test/asset-previews/prepared-1/report.json',
            'thumbnailRef': 'projects/p_test/asset-previews/prepared-1/thumbnail.png',
            'image': {'data': image_data, 'mimeType': 'image/png'}})
        self.write()
        self.assertTrue(consume(self.project)['passed'])
        thumbnail_path.write_bytes(b'wrong image')
        self.assertFalse(consume(self.project)['passed'])
        thumbnail_path.unlink()
        self.assertFalse(consume(self.project)['passed'])
        thumbnail_path.write_bytes(base64.b64decode(image_data))
        returned['result']['imageRef'] = returned['result'].pop('thumbnailRef')
        self.write()
        self.assertTrue(consume(self.project)['passed'])
        report_path.unlink()
        self.assertFalse(consume(self.project)['passed'])

    def test_trace_reference_cannot_read_another_project(self):
        other = self.project.parent/'p_other'
        other.mkdir()
        call = self.event('tool_call', name='assets_lookup', args={'query': 'worktable'})
        self.event('tool_result', name='assets_lookup', callEventId=call['eventId'], result={'items': []})
        (other/'generation-trace.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in self.events))
        report = consume(self.project, trace_ref='../p_other/generation-trace.jsonl')
        self.assertFalse(report['passed'])
        self.assertIn('scope', json.dumps(report['issues']).lower())

    def frozen_report(self):
        self.write()
        trace = (self.project/'generation-trace.jsonl').read_bytes()
        ref = f'consumer-reports/traces/{hashlib.sha256(trace).hexdigest()}.jsonl'
        path = self.project/ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(trace)
        return consume(self.project, 'g_test', trace_ref=ref)

    def restored(self, report, sample=None):
        from examples import read_generation
        entry = getattr(read_generation, 'iter_samples' if sample is None else 'read_sample', None)
        self.assertTrue(callable(entry), 'v2 references need an independent validated restoration entry point')
        return list(entry(self.project, report)) if sample is None else entry(self.project, report, sample)

    def complete_tool(self, request='request-1', **returned):
        call = self.event('tool_call', name='job', args={'requestId': request}, requestId=request)
        result = self.event('tool_result', name='job', callEventId=call['eventId'], result=returned)
        return call, result

    def test_v2_does_not_repeat_big_outcomes_or_cumulative_native_inputs(self):
        import base64
        payload = 'bounded-large-result-'*10000
        image = base64.b64encode(b'bounded-image-bytes-'*10000).decode()
        self.event('user_message', message='first input')
        self.event('codex', method='transport/sent', params={'message': {'method': 'turn/start', 'params': {
            'input': [{'type': 'image', 'mimeType': 'image/png', 'data': image}]}}})
        for unused in range(8):
            self.complete_tool(jobId='shared-job')
        self.event('operation_failed', jobId='shared-job', error=payload)
        report = self.frozen_report()
        self.assertTrue(report['passed'])
        self.assertEqual(report.get('schema'), 'sceneagent-generation-consumer-v2')
        self.assertEqual(report.get('version'), 2)
        self.assertEqual(report.get('projectId'), 'p_test')
        self.assertEqual(report.get('generationId'), 'g_test')
        encoded = json.dumps(report)
        self.assertNotIn(payload, encoded)
        self.assertNotIn(image, encoded)
        for sample in report['samples']:
            self.assertNotIn('outcomes', sample)
            self.assertNotIn('publicInputs', sample)
            self.assertIn('outcomeRefs', sample)
            self.assertIn('publicInputRefs', sample)
        self.assertLess(len(encoded), 20000)
        restored = self.restored(report)
        self.assertEqual(len(restored), 8)
        self.assertEqual(restored[0]['outcomes'][-1]['error'], payload)
        self.assertEqual(restored[-1]['publicInputs'][1]['input'][0]['data'], image)

    def test_reference_restore_matches_legacy_complete_samples_and_order(self):
        user = self.event('user_message', message='first input')
        native = self.event('codex', method='transport/sent', params={'message': {'method': 'turn/start', 'params': {
            'input': [{'type': 'text', 'text': 'repair all'}]}}})
        before = self.snapshot('old\n')
        author = self.event('codex', method='item/completed', params={'item': {'id': 'edit-1', 'type': 'fileChange'}})
        after = self.snapshot('new\n', before['sourceHash'])
        after.update(cause='edit-1', visibleInputRefs=[user['eventId']])
        call = self.event('tool_call', name='scene_build', args={'expectedSourceHash': after['sourceHash']}, requestId='build-1')
        returned = self.event('tool_result', name='scene_build', callEventId=call['eventId'], error='failed')
        outcome = self.event('operation_failed', requestId='build-1', error='actual failure')
        other = self.event('tool_call', name='job', args={'requestId': 'build-1'})
        other['generationId'] = 'g_other'
        other_result = self.event('tool_result', name='job', callEventId=other['eventId'], result={'revision': 'other-revision'})
        other_result['generationId'] = 'g_other'
        inputs = [dict(eventId=user['eventId'], type='userProvided', text='first input', sentToModel='unconfirmed'),
                  dict(eventId=native['eventId'], type='nativeInput', input=native['params']['message']['params']['input'], sentToModel='captured')]
        common = dict(requirementVersion=1, origin='unknown', agentTrainingEligible=True)
        expected = [dict(kind='edit', eventId=after['eventId'], actor='codex-native', **common,
                         actorProvenance='native_item_link', nativeActionRef=author['eventId'],
                         beforeSourceHash=before['sourceHash'], afterSourceHash=after['sourceHash'],
                         beforeSourceRef=before['snapshotRef'], afterSourceRef=after['snapshotRef'],
                         beforeSources=json.loads((self.project/before['snapshotRef']).read_text())['sourceFiles'],
                         afterSources=json.loads((self.project/after['snapshotRef']).read_text())['sourceFiles'],
                         diffs={'scene.py': '--- before/scene.py\n+++ after/scene.py\n@@ -1 +1 @@\n-old\n+new\n'},
                         visibleInputRefs=[user['eventId']], publicInputs=inputs, outcomes=[call, returned, outcome],
                         inputCoverage={'internalContext': 'unknown', 'sourceSnapshots': 'recoverable', 'sourceObservedByModel': 'unknown'}),
                    dict(kind='tool', eventId=call['eventId'], actor='codex-mcp', **common,
                         actorProvenance='local_tool_origin_label_not_authentication', visibleInputRefs=[user['eventId'], native['eventId']],
                         publicInputs=inputs, action={'name': call['name'], 'args': call['args']},
                         returned={'isError': True, 'returnedError': 'failed'}, returnEventId=returned['eventId'], outcomes=[outcome],
                         inputCoverage={'internalContext': 'unknown', 'toolParameters': 'captured', 'returnedContent': 'captured'})]
        report = self.frozen_report()
        self.assertEqual(self.restored(report), expected)
        self.assertEqual(self.restored(report, report['samples'][0]), expected[0])
        self.assertNotEqual(report['samples'][0]['publicInputRefs'], report['samples'][0]['visibleInputRefs'])
        (self.project/'generation-trace.jsonl').write_text('changed live history')
        self.assertEqual(self.restored(report), expected)

    def test_linkage_remains_forward_only_transitive_and_isolated_between_samples(self):
        self.complete_tool(jobId='job-a')
        missed = self.event('operation_completed', revision='rev-later', result={'episodeId': 'not-backfilled'})
        expanded = self.event('operation_completed', jobId='job-a', result={'revision': 'rev-later'})
        transitive = self.event('operation_failed', revision='rev-later', error='real failure')
        self.complete_tool(request='request-b', jobId='job-b')
        excluded = self.event('operation_completed', revision='rev-later', result={'episodeId': 'unrelated'})
        own = self.event('operation_completed', jobId='job-b', result={'episodeId': 'episode-b'})
        report = self.frozen_report()
        samples = self.restored(report)
        self.assertEqual([event['eventId'] for event in samples[0]['outcomes']],
                         [expanded['eventId'], transitive['eventId'], excluded['eventId']])
        self.assertNotIn(missed['eventId'], [event['eventId'] for event in samples[0]['outcomes']])
        self.assertEqual(samples[1]['outcomes'], [own])

    def test_links_are_extracted_once_instead_of_for_every_sample_suffix(self):
        import cProfile
        import pstats
        nested = {'nested': [{'text': 'irrelevant', 'values': [{'text': str(i)} for i in range(30)]} for unused in range(10)]}
        for unused in range(15):
            self.complete_tool(jobId='job-a', payload=nested)
        self.event('operation_completed', jobId='job-a', result={'revision': 'rev-a'})
        self.write()
        profiler = cProfile.Profile()
        profiler.runcall(consume, self.project, 'g_test')
        recursive = sum(value[1] for key, value in pstats.Stats(profiler).stats.items() if key[2] == 'links')
        self.assertLess(recursive, 12000, 'same nested tool content was recursively re-scanned for every sample')

    def test_restore_rejects_missing_tampered_or_outside_frozen_trace(self):
        self.complete_tool(jobId='job-a')
        report = self.frozen_report()
        frozen = self.project/report['traceRef']
        original = frozen.read_bytes()
        frozen.write_bytes(original+b'\n')
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.restored(report)
        frozen.unlink()
        with self.assertRaises((ValueError, OSError)):
            self.restored(report)
        frozen.write_bytes(original)
        import copy
        for ref in ('../p_other/private.jsonl', 'projects/p_other/private.jsonl'):
            bad = copy.deepcopy(report)
            bad['traceRef'] = ref
            with self.assertRaisesRegex(ValueError, 'scope'):
                self.restored(bad)
        other = self.project.parent/'p_other'
        other.mkdir()
        (other/'copied.jsonl').write_bytes(original)
        (self.project/'symlink.jsonl').symlink_to(other/'copied.jsonl')
        bad = copy.deepcopy(report)
        bad['traceRef'] = 'symlink.jsonl'
        with self.assertRaisesRegex(ValueError, 'scope'):
            self.restored(bad)

    def test_restore_rejects_unknown_duplicate_cross_generation_and_wrong_type_refs(self):
        import copy
        user = self.event('user_message', message='input')
        call, result = self.complete_tool(jobId='job-a')
        outcome = self.event('operation_completed', jobId='job-a', result={'revision': 'rev-a'})
        other = self.event('operation_completed', jobId='job-a', result={'revision': 'other'})
        other['generationId'] = 'g_other'
        report = self.frozen_report()
        for field, refs in [('outcomeRefs', ['missing']), ('outcomeRefs', [outcome['eventId'], outcome['eventId']]),
                            ('outcomeRefs', [other['eventId']]), ('outcomeRefs', [user['eventId']]),
                            ('publicInputRefs', [result['eventId']]),
                            ('outcomeRefs', [dict(eventId=outcome['eventId'], traceHash='0'*64)])]:
            bad = copy.deepcopy(report)
            bad['samples'][0][field] = refs
            with self.subTest(field=field, refs=refs), self.assertRaises(ValueError):
                self.restored(bad)
        bad = copy.deepcopy(report)
        bad['projectId'] = 'p_other'
        with self.assertRaisesRegex(ValueError, 'project|scope'):
            self.restored(bad)
        rows = self.events+[copy.deepcopy(outcome)]
        (self.project/report['traceRef']).write_text(''.join(json.dumps(row)+'\n' for row in rows))
        bad = copy.deepcopy(report)
        bad['traceHash'] = hashlib.sha256((self.project/report['traceRef']).read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'duplicate|identity'):
            self.restored(bad)

    def test_restore_rejects_unpaired_tools_even_with_a_rehashed_trace(self):
        call, result = self.complete_tool(jobId='job-a')
        report = self.frozen_report()
        result['callEventId'] = 'missing'
        frozen = self.project/report['traceRef']
        frozen.write_text(''.join(json.dumps(row)+'\n' for row in self.events))
        report['traceHash'] = hashlib.sha256(frozen.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'pair|matching'):
            self.restored(report)

    def test_restore_rejects_cross_generation_pairing_without_filter(self):
        call, result = self.complete_tool(jobId='job-a')
        result['generationId'] = 'g_other'
        self.write()
        report = consume(self.project)
        self.assertFalse(report['passed'])
        self.assertIn('unpaired_tool_result', [issue['code'] for issue in report['issues']])

    def test_legacy_inline_report_still_reads_without_modification_and_unknown_schema_fails(self):
        import copy
        legacy = dict(passed=True, samples=[dict(kind='tool', eventId='legacy', publicInputs=[], outcomes=[], returned={'full': 'content'})])
        path = self.project/'legacy-report.json'
        path.write_text(json.dumps(legacy))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self.restored(legacy), legacy['samples'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        for schema in ('sceneagent-generation-consumer-v3', None):
            bad = copy.deepcopy(legacy)
            bad['schema'] = schema
            with self.assertRaisesRegex(ValueError, 'schema|version'):
                self.restored(bad)

    def test_archived_native_image_ref_requires_bytes_digest_and_project_scope(self):
        import base64
        data = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a9sAAAAASUVORK5CYII=')
        relative = '.trace/inputs/archived.png'
        path = self.project/relative
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        image = {'type': 'image', 'mimeType': 'image/png', 'ref': relative, 'sha256': hashlib.sha256(data).hexdigest()}
        self.event('codex', method='transport/sent', params={'message': {'method': 'turn/start', 'params': {'input': [image]}}})
        self.complete_tool(jobId='job-a')
        self.write()
        self.assertTrue(consume(self.project)['passed'])
        report = self.frozen_report()
        self.assertEqual(self.restored(report)[0]['publicInputs'][0]['input'], [image])
        path.write_bytes(b'bad')
        self.assertFalse(consume(self.project)['passed'])
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.restored(report)
        path.write_bytes(data)
        image['ref'] = '../p_other/frame.png'
        self.write()
        self.assertFalse(consume(self.project)['passed'])
        image.pop('ref')
        self.write()
        self.assertFalse(consume(self.project)['passed'])

    def test_default_consume_freezes_once_and_restore_never_reads_active_trace(self):
        import copy
        self.complete_tool(jobId='job-a')
        self.write()
        report = consume(self.project)
        self.assertNotEqual(report['traceRef'], 'generation-trace.jsonl')
        frozen = self.project/report['traceRef']
        before = frozen.stat().st_mtime_ns
        self.assertEqual(consume(self.project)['traceRef'], report['traceRef'])
        self.assertEqual(frozen.stat().st_mtime_ns, before)
        bad = copy.deepcopy(report)
        bad['traceRef'] = 'generation-trace.jsonl'
        with self.assertRaisesRegex(ValueError, 'active|frozen'):
            self.restored(bad)
        (self.project/'active-alias.jsonl').symlink_to(self.project/'generation-trace.jsonl')
        bad['traceRef'] = 'active-alias.jsonl'
        with self.assertRaisesRegex(ValueError, 'active|frozen'):
            self.restored(bad)
        (self.project/'generation-trace.jsonl').write_text('changed live trace')
        self.assertEqual(self.restored(report)[0]['returned'], {'jobId': 'job-a'})

    def test_shared_freezer_and_consumer_bind_same_bytes_and_reject_occupied_hash_file(self):
        from examples.read_generation import freeze_trace
        self.complete_tool(jobId='job-a')
        self.write()
        ref, digest, raw = freeze_trace(self.project)
        report = consume(self.project, 'g_test')
        self.assertEqual((ref, digest), (report['traceRef'], report['traceHash']))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        self.assertEqual((self.project/ref).read_bytes(), raw)
        self.complete_tool(request='request-b', jobId='job-b')
        self.write()
        changed_ref, changed_digest, changed_raw = freeze_trace(self.project)
        self.assertNotEqual(changed_ref, ref)
        self.assertNotEqual(changed_digest, digest)
        self.assertEqual((self.project/ref).read_bytes(), raw)
        self.assertEqual((self.project/changed_ref).read_bytes(), changed_raw)
        (self.project/changed_ref).write_bytes(b'occupied wrong bytes')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            freeze_trace(self.project)
        self.assertFalse(consume(self.project, 'g_test')['passed'])

    def test_restore_rejects_deleted_failure_or_added_unrelated_outcome_and_deleted_public_input(self):
        import copy
        self.event('user_message', message='complete original input')
        self.complete_tool(jobId='job-a')
        outcome = self.event('operation_failed', jobId='job-a', error='must preserve failure')
        unrelated = self.event('operation_completed', jobId='other-job', result={})
        report = self.frozen_report()
        for field, refs in [('outcomeRefs', []), ('outcomeRefs', [outcome['eventId'], unrelated['eventId']]),
                            ('publicInputRefs', [])]:
            bad = copy.deepcopy(report)
            bad['samples'][0][field] = refs
            with self.subTest(field=field, refs=refs), self.assertRaisesRegex(ValueError, 'complete|prefix'):
                self.restored(bad)

    def test_restore_rechecks_original_source_snapshot_even_when_only_tool_samples_exist(self):
        initial = self.snapshot('original source')
        self.complete_tool(jobId='job-a')
        report = self.frozen_report()
        self.assertTrue(report['passed'])
        (self.project/initial['snapshotRef']).write_text(json.dumps({'sourceHash': initial['sourceHash'], 'sourceFiles': {'scene.py': 'changed'}}))
        with self.assertRaisesRegex(ValueError, 'source hash'):
            self.restored(report)


    def test_unfiltered_report_keeps_null_scope_and_complete_input_prefix_across_generations(self):
        first = self.event('user_message', message='first generation input')
        call1, result1 = self.complete_tool(request='request-a', jobId='job-a')
        second = self.event('user_message', message='second generation input')
        second['generationId'] = 'g_other'
        call2, result2 = self.complete_tool(request='request-b', jobId='job-b')
        call2['generationId'] = result2['generationId'] = 'g_other'
        self.write()
        report = consume(self.project)
        self.assertIsNone(report['generationId'])
        self.assertTrue(report['passed'])
        samples = self.restored(report)
        self.assertEqual([sample['eventId'] for sample in samples], [call1['eventId'], call2['eventId']])
        self.assertEqual([item['eventId'] for item in samples[0]['publicInputs']], [first['eventId']])
        self.assertEqual([item['eventId'] for item in samples[1]['publicInputs']], [first['eventId'], second['eventId']])
        self.assertEqual([sample['returned'] for sample in samples], [result1['result'], result2['result']])

    def test_result_body_has_priority_over_error_and_changed_tool_body_is_rejected(self):
        import copy
        call, result = self.complete_tool(jobId='job-a')
        result['error'] = 'unused when actual result exists'
        report = self.frozen_report()
        self.assertEqual(self.restored(report)[0]['returned'], result['result'])
        for field, value in [('action', {'name': 'job', 'args': {'jobId': 'different'}}), ('returned', {'jobId': 'different'})]:
            bad = copy.deepcopy(report)
            bad['samples'][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'content'):
                self.restored(bad)

    def test_missing_event_identity_is_a_failed_report_and_all_refs_validate_before_first_yield(self):
        import copy
        self.complete_tool(jobId='job-a')
        self.complete_tool(request='request-b', jobId='job-b')
        report = self.frozen_report()
        bad = copy.deepcopy(report)
        bad['samples'][1]['outcomeRefs'] = ['missing']
        from examples.read_generation import iter_samples
        with self.assertRaisesRegex(ValueError, 'unknown event'):
            next(iter_samples(self.project, bad))
        del self.events[-1]['eventId']
        self.write()
        self.assertFalse(consume(self.project)['passed'])


    def test_active_trace_itself_can_be_a_symlink_and_all_its_aliases_still_require_freezing(self):
        import copy
        self.complete_tool(jobId='job-a')
        self.write()
        active = self.project/'generation-trace.jsonl'
        target = self.project/'live-target.jsonl'
        active.rename(target)
        active.symlink_to(target)
        (self.project/'another-alias.jsonl').symlink_to(active)
        reports = []
        for ref in ('generation-trace.jsonl', 'live-target.jsonl', 'another-alias.jsonl'):
            report = consume(self.project, 'g_test', trace_ref=ref)
            reports.append(report)
            with self.subTest(input=ref):
                self.assertTrue(report['passed'])
                self.assertTrue(report['traceRef'].startswith('consumer-reports/traces/'))
        for ref in ('generation-trace.jsonl', 'live-target.jsonl', 'another-alias.jsonl'):
            bad = copy.deepcopy(reports[0])
            bad['traceRef'] = ref
            with self.subTest(restore=ref), self.assertRaisesRegex(ValueError, 'active|frozen'):
                self.restored(bad)
        target.write_text('changed actual active target')
        for report in reports:
            with self.subTest(frozen=report['traceRef']):
                self.assertEqual(self.restored(report)[0]['returned'], {'jobId': 'job-a'})

    def test_missing_visible_input_in_frozen_edit_fails_before_any_restoration(self):
        self.event('codex', method='transport/sent', params={'message': {'method': 'turn/start', 'params': {
            'input': [{'type': 'text', 'text': 'change source'}]}}})
        before = self.snapshot('old source')
        self.event('codex', method='item/completed', params={'item': {'id': 'edit-1', 'type': 'fileChange'}})
        after = self.snapshot('new source', before['sourceHash'])
        after.update(cause='edit-1', visibleInputRefs=['missing'])
        call = self.event('tool_call', name='scene_build', args={'expectedSourceHash': after['sourceHash']}, requestId='build-1')
        self.event('tool_result', name='scene_build', callEventId=call['eventId'], result={'revision': 'rev-1'})
        report = self.frozen_report()
        self.assertFalse(report['passed'])
        self.assertIn('missing_input', [issue['code'] for issue in report['issues']])
        self.assertEqual(report['sampleCount'], 2)
        from examples.read_generation import iter_samples, read_sample
        for method in ('first_yield', 'one_sample'):
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, 'visible input'):
                if method == 'first_yield':
                    next(iter_samples(self.project, report))
                else:
                    read_sample(self.project, report, report['samples'][1])



if __name__ == '__main__':
    unittest.main()
