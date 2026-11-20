"""SYSTEM_VERIFICATION_ONLY: generated Python grading over finite CPU facts."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtime import worker
from runtime.scene import Task, World


SOURCE = '''def evaluate(task, facts, state):
    count = 0 if state is None else state['count']
    count += int(facts['phase'] == 'transition')
    return {'state': {'count': count}, 'result': {
        'success': count >= task.parameters['count'],
        'terminated': count >= task.parameters['count'],
        'reward': float(count), 'details': {'count': count}}}
'''


def selected(source=SOURCE):
    return {'sourceRef': 'rubrics.py', 'sourceDigest': hashlib.sha256(source.encode()).hexdigest(),
            'helperDigests': {}, 'sourceFiles': {'rubrics.py': source}}


def task():
    return Task('count_events', instruction='System fixture: count committed events',
                bindings={'object': 'cube'}, parameters={'count': 2},
                rubric={'sourceRef': 'rubrics.py', 'helperRefs': []})


class GeneratedRubricTests(unittest.TestCase):
    def session(self, source=SOURCE, **kwargs):
        command = [sys.executable, '-c', 'from runtime.worker import rubric_entry; rubric_entry()']
        replacement = patch.object(worker, 'controller_command', return_value=command)
        replacement.start()
        self.addCleanup(replacement.stop)
        session = worker.RubricSession(task(), selected(source), **kwargs)
        self.addCleanup(session.close)
        return session

    def array_session(self):
        import numpy as np
        from runtime.records import _array_reference
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        references = [_array_reference(root,np.arange(90,dtype=np.float32)+offset) for offset in (0,1)]
        source = '''from pathlib import Path
import numpy as np
def evaluate(task, facts, state):
    refs = facts['refs']
    reward = float(np.load(refs[0]['ref'], allow_pickle=False).sum()) if refs else 0.
    return {'state': {'count': 1 if state is None else state['count']+1}, 'result': {
        'success': False, 'terminated': False, 'reward': reward,
        'details': {'visible': sorted(str(path.relative_to(Path.cwd())) for path in Path.cwd().rglob('*.npy'))}}}
'''
        return root,references,self.session(source,record_root=root)

    def test_repeated_array_refs_are_verified_once_and_current_copies_are_reused(self):
        from runtime import records
        root,references,session = self.array_session();reference = references[0]
        with patch.object(records,'read_array',wraps=records.read_array) as read, \
             patch.object(worker.shutil,'copyfile',wraps=worker.shutil.copyfile) as copy:
            first = session.preview('reset',{'refs':[reference,deepcopy(reference),reference]})
            self.assertEqual(first['result']['reward'],4005.)
            self.assertEqual(read.call_count,2)
            self.assertEqual(copy.call_count,1)
            session.accept(first)
            identity = (session.array_root/reference['ref']).stat().st_ino
            second = session.preview('transition',{'refs':[reference]})
            self.assertEqual(second['result']['reward'],4005.)
            self.assertEqual(copy.call_count,1)
            self.assertEqual((session.array_root/reference['ref']).stat().st_ino,identity)
            self.assertEqual(read.call_count,4)

    def test_rubric_view_and_cache_drop_refs_outside_the_current_facts(self):
        _,references,session = self.array_session()
        with patch.object(worker.shutil,'copyfile',wraps=worker.shutil.copyfile) as copy:
            first = session.preview('reset',{'refs':references});session.accept(first)
            self.assertEqual(first['result']['details']['visible'],sorted(ref['ref'] for ref in references))
            second = session.preview('transition',{'refs':[references[1]]});session.accept(second)
            self.assertEqual(second['result']['details']['visible'],[references[1]['ref']])
            self.assertEqual(copy.call_count,2)
            self.assertEqual(set(session._array_cache),{references[1]['ref']})
            empty = session.preview('transition',{'refs':[]})
            self.assertEqual(empty['result']['details']['visible'],[])
            self.assertEqual(session._array_cache,{})

    def test_conflicting_descriptions_of_one_array_path_fail_before_scoring(self):
        for field,value in (('sha256','0'*64),('dtype','float64'),('shape',[30,3])):
            with self.subTest(field=field):
                _,references,session = self.array_session();reference = references[0]
                with patch.object(session,'_exchange',wraps=session._exchange) as exchange:
                    with self.assertRaisesRegex(ValueError,'conflicting array reference'):
                        session.preview('reset',{'refs':[reference,{**reference,field:value}]})
                    exchange.assert_not_called()
                self.assertIsNone(session.state);self.assertIsNone(session.last_result)

    def test_reused_source_or_view_changes_cannot_publish_an_old_staged_grade(self):
        for location in ('source','view'):
            for mutation in ('content','replacement','symlink'):
                with self.subTest(location=location,mutation=mutation):
                    root,references,session = self.array_session();reference = references[0]
                    first = session.preview('reset',{'refs':[reference]});session.accept(first)
                    pending = session.preview('transition',{'refs':[reference]})
                    target = (root if location=='source' else session.array_root)/reference['ref']
                    original = target.read_bytes()
                    if mutation=='content':target.write_bytes(original[:-1]+bytes([original[-1]^1]))
                    elif mutation=='replacement':
                        temporary = target.with_suffix('.replacement');temporary.write_bytes(original);temporary.replace(target)
                    else:
                        temporary = root/'linked.npy';temporary.write_bytes(original)
                        target.unlink();target.symlink_to(temporary)
                    with patch.object(session,'_exchange',wraps=session._exchange) as exchange:
                        with self.assertRaises((ValueError,OSError)):
                            session.preview('transition',{'refs':[reference]})
                        exchange.assert_not_called()
                    self.assertEqual(session.state,first['state'])
                    self.assertEqual(session.last_result,first['result'])
                    with self.assertRaisesRegex(ValueError,'unchanged|unavailable'):
                        session.accept(pending)

    def test_preview_does_not_advance_private_state_before_accept(self):
        session = self.session()
        reset = session.preview('reset', {'time': 0})
        session.accept(reset)
        first = session.preview('transition', {'events': ['first']})
        self.assertFalse(session.last_result['success'])
        same = session.preview('transition', {'events': ['first']})
        self.assertEqual(first, same)
        session.accept(first)
        last = session.preview('transition', {'events': ['second']})
        self.assertTrue(last['result']['terminated'])
        session.accept(last)
        self.assertEqual(session.last_result['reward'], 2.)
        with self.assertRaisesRegex(ValueError, 'terminated'):
            session.preview('transition', {})

    def test_failed_task_can_terminate_and_finish_is_not_an_entrypoint(self):
        source = "def evaluate(task, facts, state):\n    return {'state': None, 'result': {'success': False, 'terminated': True, 'reward': -1., 'details': {}}}\n"
        session = self.session(source)
        result = session.preview('reset', {})
        session.accept(result)
        self.assertFalse(session.last_result['success'])
        self.assertTrue(session.last_result['terminated'])
        with self.assertRaises(ValueError):
            session.preview('finish', {})

    def test_invalid_or_unbounded_rubric_output_is_unavailable(self):
        for expression in ("{'state': None, 'result': {'success': 1, 'terminated': False, 'reward': None, 'details': {}}}",
                           "{'state': None, 'result': {'success': None, 'terminated': False, 'reward': float('nan'), 'details': {}}}",
                           "{'state': 'x'*2000000, 'result': {'success': None, 'terminated': False, 'reward': None, 'details': {}}}"):
            with self.subTest(expression=expression):
                session = self.session('def evaluate(task, facts, state):\n    return '+expression+'\n')
                with self.assertRaises(worker.RubricUnavailable):
                    session.preview('reset', {})

    def test_rubric_timeout_is_bounded_and_reaped(self):
        session = self.session('def evaluate(task, facts, state):\n    while True: pass\n', max_seconds=.3)
        with self.assertRaises(worker.RubricUnavailable):
            session.preview('reset', {})
        self.assertIsNotNone(session.process.poll())

    def test_source_closure_and_declared_expected_are_generated_data(self):
        world = World(); world.box('cube', [.1]*3)
        definition = task()
        definition.evaluation = {'cases': [
            {'id': 'one', 'sourceRef': 'a.py', 'helperRefs': [],
             'expected': {'success': False}, 'requirementRefs': ['fixture']},
            {'id': 'two', 'sourceRef': 'b.py', 'helperRefs': [],
             'expected': {'reward': -5.}, 'requirementRefs': ['fixture']},
            {'id': 'three', 'sourceRef': 'c.py', 'helperRefs': [],
             'expected': {'success': False}, 'requirementRefs': ['fixture']}]}
        definition.bind(world)
        files = {**selected()['sourceFiles'], 'a.py': '', 'b.py': '', 'c.py': ''}
        self.assertEqual(set(worker.evaluation_sources(definition, files)), {'one', 'two', 'three'})
        changed = deepcopy(files); changed['rubrics.py'] += '# changed\n'
        with self.assertRaisesRegex(ValueError, 'digest'):
            worker.rubric_sources(definition, changed, {'rubrics.py': selected()['sourceDigest']})

    def test_expert_has_only_actual_helpers_and_cannot_import_private_entries(self):
        definition = task()
        files = {'expert.py': 'from shared import run\n', 'shared.py': 'def run(env, task): pass\n',
                 'rubrics.py': SOURCE, 'scene.py': '', 'tasks.py': '', 'unused.py': ''}
        hashes = {name: hashlib.sha256(source.encode()).hexdigest() for name, source in files.items()}
        closure = worker.expert_sources(definition, files, hashes)
        self.assertEqual(set(closure['sourceFiles']), {'expert.py', 'shared.py'})
        files['expert.py'] = 'import rubrics\n'
        hashes['expert.py'] = hashlib.sha256(files['expert.py'].encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'private'):
            worker.expert_sources(definition, files, hashes)

    def test_staged_state_cannot_be_modified_before_commit(self):
        session = self.session()
        staged = session.preview('reset', {})
        staged['state']['count'] = 500
        with self.assertRaisesRegex(ValueError, 'unchanged'):
            session.accept(staged)
        self.assertIsNone(session.last_result)

    def test_declared_package_helpers_run_from_their_frozen_closure(self):
        definition = task()
        definition.rubric = {'sourceRef': 'grading/score.py',
            'helperRefs': ['grading/__init__.py', 'grading/numbers.py']}
        files = {'grading/__init__.py': '', 'grading/numbers.py': 'REWARD = 17.\n',
            'grading/score.py': "from .numbers import REWARD\ndef evaluate(task, facts, state):\n    return {'state': None, 'result': {'success': False, 'terminated': False, 'reward': REWARD, 'details': {}}}\n"}
        closure = worker.rubric_sources(definition, files)
        command = [sys.executable, '-c', 'from runtime.worker import rubric_entry; rubric_entry()']
        with patch.object(worker, 'controller_command', return_value=command):
            session = worker.RubricSession(definition, closure)
            try:
                self.assertEqual(session.preview('reset', {})['result']['reward'], 17.)
            finally:
                session.close()

    def test_profile_helper_is_relative_to_real_rubric_file_with_array_cwd(self):
        definition = task()
        definition.rubric = {'sourceRef': 'grading/score.py', 'helperRefs': ['grading/arm.robot.json']}
        files = {'grading/arm.robot.json': '{"schema":"sceneagent-robot-profile-v1","model":{"resourceRef":"fixture"},"ports":{},"streams":{}}',
            'grading/score.py': """from pathlib import Path
import json
PROFILE = json.loads(Path(__file__).with_name('arm.robot.json').read_text())
def evaluate(task, facts, state):
    assert Path(__file__).name == 'score.py'
    assert Path.cwd().name.startswith('rubric-facts-')
    return {'state': None, 'result': {'success': False, 'terminated': False, 'reward': None,
        'details': {'schema': PROFILE['schema']}}}
"""}
        closure = worker.rubric_sources(definition, files)
        command = [sys.executable, '-c', 'from runtime.worker import rubric_entry; rubric_entry()']
        with patch.object(worker, 'controller_command', return_value=command):
            session = worker.RubricSession(definition, closure)
            try:
                self.assertEqual(session.preview('reset', {})['result']['details']['schema'],
                    'sceneagent-robot-profile-v1')
            finally:
                session.close()

    def test_grading_protocol_never_dispatches_robot_action_messages(self):
        source = "import os\ndef evaluate(task, facts, state):\n    os.write(1, b'{\"action\":\"step\"}\\n')\n    while True: pass\n"
        session = self.session(source, max_seconds=.3)
        with self.assertRaises(worker.RubricUnavailable):
            session.preview('reset', {})
        self.assertIsNotNone(session.process.poll())


if __name__ == '__main__':
    unittest.main()
