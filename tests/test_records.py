import json
from copy import deepcopy
from runtime.scene import Task, World
from runtime.records import cycle_facts, _physical_digest
from pathlib import Path
import tempfile
import unittest

from runtime.records import Recorder, load_batch, atomic_json

HOLD = [0, -.4, 0, -2, 0, 1.6, .7854, .08]  # Preserved CPU fixture export, not a runtime default.
COMMAND = {'arm':HOLD}
from tests.test_native_records import IO as NATIVE_IO
BASE_IO = deepcopy(NATIVE_IO)
BASE_IO['ports']['arm'].update(shape=[8],units=['rad']*7+['m'],limits=[[-4,4]]*7+[[0,.08]])
BASE_IO['ports']['arm']['source']['joints']=[f'fixture_joint_{i}' for i in range(8)]
BASE_IO['streams'].pop('nodes')
BASE_IO['streams']['q'].update(shape=[9],units=['rad']*7+['m']*2)
BASE_IO['streams']['q']['source']['joints']=[f'fixture_joint_{i}' for i in range(9)]
BASE_IO['streams']['dq'] = {**deepcopy(BASE_IO['streams']['q']), 'units':['rad/s']*7+['m/s']*2}
BASE_IO['streams']['dq']['source']['reader']='joint_velocities'


class RubricFixture:
    """SYSTEM_VERIFICATION_ONLY transactional scorer, not a generated task."""
    descriptor = {'sourceRef': 'rubrics.py', 'sourceDigest': 'a'*64, 'helperDigests': {}}

    def __init__(self, *, terminal=False, error=False, success=False):
        self.state, self.last_result, self.calls = None, None, []
        self.terminal, self.error = terminal, error
        self.success = success

    def preview(self, phase, facts):
        self.calls.append((phase, facts))
        if self.error and phase == 'transition':
            raise RuntimeError('synthetic rubric unavailable')
        return {'state': {'cycles': 0 if phase == 'reset' else (self.state or {})['cycles']+1},
                'result': {'success': self.success, 'terminated': self.terminal and phase == 'transition',
                           'reward': None, 'details': {}}}

    def accept(self, staged):
        self.state, self.last_result = staged['state'], staged['result']


def observation(time, frame=None):
    value = {'time':time, 'simulationTime':time+1, 'timeOrigin':1, 'physicsStep':round(time*500),
             'stageId':'stage1', 'streams':{'q':[0]*9,'dq':[0]*9}, 'streamStatus':{}, 'cameraFrames':{}}
    if frame is not None:value['cameraFrames']={'front':frame}
    return value


class GeneratedRecordTransactions(unittest.TestCase):
    def test_committed_observation_publishes_only_after_reset_and_full_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory, io=BASE_IO)
            self.assertIsNone(recorder.committed_observation)
            recorder.begin({'observation':observation(0), 'truth':deepcopy(observation(0))}, RubricFixture())
            self.assertEqual(recorder.committed_observation, observation(0))
            copy = recorder.committed_observation; copy['streams']['q'][0] = 1
            self.assertEqual(recorder.committed_observation['streams']['q'][0], 0)
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            self.assertEqual(recorder.committed_observation, observation(.02))
            recorder.close('completed')
            self.assertEqual(recorder.committed_observation, observation(.02))

    def test_failed_publication_or_accept_does_not_advance_pixel_projection(self):
        from unittest.mock import patch
        class AcceptFailure(RubricFixture):
            def accept(self, staged):
                if staged['state']['cycles'] > 0:raise RuntimeError('CPU private state accept failed')
                super().accept(staged)
        for kind in ('index', 'accept'):
            with tempfile.TemporaryDirectory() as directory, self.subTest(kind=kind):
                recorder = Recorder(directory, io=BASE_IO)
                recorder.begin({'observation':observation(0), 'truth':deepcopy(observation(0))}, AcceptFailure() if kind == 'accept' else RubricFixture())
                if kind == 'index':
                    with patch('runtime.records.atomic_json',side_effect=OSError('CPU publication failed')):
                        with self.assertRaises(OSError):recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
                else:
                    with self.assertRaisesRegex(RuntimeError,'accept'):recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
                self.assertEqual(recorder.committed_observation, observation(0))
                recorder.close('interrupted')
        class ResetAcceptFailure(RubricFixture):
            def accept(self, staged):raise RuntimeError('CPU reset accept failed')
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory, io=BASE_IO)
            with self.assertRaisesRegex(RuntimeError,'reset accept'):
                recorder.begin({'observation':observation(0), 'truth':deepcopy(observation(0))}, ResetAcceptFailure())
            self.assertIsNone(recorder.committed_observation)
            recorder.close('interrupted')

    def test_pixel_projection_uses_the_committed_copy_not_a_mutated_caller_argument(self):
        after = observation(.02)
        class MutatingAccept(RubricFixture):
            def accept(self, staged):
                if staged['state']['cycles'] > 0:after['streams']['q'][0] = 1
                super().accept(staged)
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory, io=BASE_IO)
            recorder.begin({'observation':observation(0), 'truth':deepcopy(observation(0))}, MutatingAccept())
            recorder.commit(observation(0),COMMAND,COMMAND,after,10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(after))
            self.assertEqual(after['streams']['q'][0],1)
            self.assertEqual(recorder.committed_observation['streams']['q'][0],0)
            recorder.close('completed')

    def begin(self, directory, **options):
        recorder, session = Recorder(Path(directory), io=BASE_IO), RubricFixture(**options)
        recorder.begin({'observation': observation(0), 'truth': deepcopy(observation(0))}, session)
        return recorder, session

    def test_reset_is_recorded_once_and_close_does_not_rescore(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder, session = self.begin(directory)
            with self.assertRaisesRegex(ValueError, 'already|once'):
                recorder.begin({}, session)
            recorder.close('success')
            index = json.loads((Path(directory)/'records.json').read_text())
            self.assertEqual([phase for phase, _ in session.calls], ['reset'])
            self.assertEqual(index['initialSnapshot']['observation'], observation(0))
            self.assertFalse(index['terminated'])
            self.assertFalse(index['initialRubric']['result']['success'])

    def test_failed_index_never_accepts_the_previewed_state(self):
        from copy import deepcopy
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            recorder, session = self.begin(directory)
            before = deepcopy(session.state)
            with patch('runtime.records.atomic_json', side_effect=OSError('index publication')):
                with self.assertRaisesRegex(OSError, 'index publication'):
                    recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            self.assertEqual(session.state, before)
            self.assertEqual(recorder.cycles, 0)
            recorder.close('controller_error')

    def test_rubric_error_preserves_actual_cycle_with_unknown_score(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder, session = self.begin(directory, error=True)
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            recorder.close('controller_error')
            from runtime.records import read_transitions
            rows, index, tail = read_transitions(directory)
            self.assertEqual((len(rows), index['physicalSteps'], tail), (1, 10, False))
            self.assertEqual(rows[0]['scoreStatus'], 'unavailable')
            self.assertIsNone(rows[0]['rubricResult']['success'])
            self.assertEqual(session.state, {'cycles': 0})
            self.assertTrue(recorder.episode_done)

    def test_failure_termination_stops_without_turning_into_success(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder, session = self.begin(directory, terminal=True)
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            with self.assertRaisesRegex(RuntimeError, 'ended|terminated'):
                recorder.commit(observation(.02), COMMAND, COMMAND, observation(.04), 10, truth_before=deepcopy(observation(.02)), truth_after=deepcopy(observation(.04)))
            recorder.close('completed')
            batch = load_batch(directory)
            self.assertTrue(batch['terminated'])
            self.assertFalse(batch['lastResult']['success'])
            self.assertEqual(session.state, {'cycles': 1})

    def test_reset_can_terminate_a_zero_action_episode_without_regrading_on_close(self):
        class ResetTerminal(RubricFixture):
            def preview(self, phase, facts):
                staged = super().preview(phase, facts)
                staged['result']['terminated'] = True
                return staged
        with tempfile.TemporaryDirectory() as directory:
            recorder, session = Recorder(Path(directory), io=BASE_IO), ResetTerminal()
            recorder.begin({'observation':observation(0), 'truth':deepcopy(observation(0))}, session)
            with self.assertRaisesRegex(RuntimeError, 'ended'):
                recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            recorder.close('completed')
            from runtime.records import read_transitions
            rows,index,tail = read_transitions(directory)
            self.assertEqual(rows, [])
            self.assertTrue(index['terminated'])
            self.assertFalse(index['lastResult']['success'])
            self.assertEqual(len(session.calls), 1)

    def test_unique_reset_and_cross_cycle_host_facts_cannot_be_substituted(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder, _ = self.begin(directory)
            recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            recorder.commit(observation(.02),COMMAND,COMMAND,observation(.04),10, truth_before=deepcopy(observation(.02)), truth_after=deepcopy(observation(.04)))
            recorder.close('completed')
            path = Path(directory)/'transitions.jsonl'
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[1]['observation']['streams']['q'][0] = .2
            from runtime.records import cycle_facts, _physical_digest
            rows[1]['facts'] = cycle_facts(rows[1]); rows[1]['factsDigest'] = _physical_digest(rows[1]['facts'])
            data = ''.join(json.dumps(row)+'\n' for row in rows).encode(); path.write_bytes(data)
            index = json.loads((Path(directory)/'records.json').read_text()); index['committedBytes'] = len(data)
            atomic_json(Path(directory)/'records.json',index)
            with self.assertRaisesRegex(ValueError, 'boundary'): load_batch(directory)


class RollingRecordCacheTests(unittest.TestCase):
    def fixture(self, directory):
        import numpy as np
        io = deepcopy(BASE_IO)
        io['streams']['nodes'] = deepcopy(NATIVE_IO['streams']['nodes'])
        io['streams']['nodes']['visibleToPolicy'] = True
        values = np.arange(210, dtype=np.float32).reshape(70, 3)
        recorder = Recorder(directory, io=io)
        initial = self.state(0, values)
        recorder.begin({'observation':initial, 'truth':deepcopy(initial)}, RubricFixture())
        self.addCleanup(recorder.file.close)
        return recorder, values

    def state(self, time, values):
        result = observation(time)
        result['streams']['nodes'] = values
        return result

    def commit(self, recorder, time, values):
        before = recorder.committed_observation
        truth = recorder.index['initialSnapshot']['truth'] if not recorder.cycles else recorder._committed_truth
        after = self.state(time, values)
        recorder.commit(before, COMMAND, COMMAND, after, 10,
            truth_before=deepcopy(truth), truth_after=deepcopy(after))

    def test_unchanged_arrays_reuse_reset_content_and_keep_both_cycle_commit_barriers(self):
        import numpy as np
        from runtime import records
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            recorder, values = self.fixture(directory)
            with patch.object(np, 'save', wraps=np.save) as saved, \
                 patch.object(records.os, 'fsync', wraps=records.os.fsync) as synced:
                for cycle in range(1, 5):
                    self.commit(recorder, cycle*.02, values)
            self.assertEqual(saved.call_count, 0)
            self.assertEqual(synced.call_count, 8)
            recorder.close('completed')
            rows, index, tail = records.read_transitions(directory)
            self.assertEqual((len(rows), index['physicalSteps'], tail), (4, 40, False))

    def test_in_place_source_mutation_freezes_new_content_instead_of_reusing_object_identity(self):
        import numpy as np
        from runtime import records
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            recorder, values = self.fixture(directory)
            original = recorder.index['initialSnapshot']['truth']['streams']['nodes']
            values += 1
            with patch.object(np, 'save', wraps=np.save) as saved:
                self.commit(recorder, .02, values)
            changed = recorder.committed_observation['streams']['nodes']
            self.assertNotEqual(changed['sha256'], original['sha256'])
            np.testing.assert_array_equal(records.read_array(directory, changed), values)
            np.testing.assert_array_equal(records.read_array(directory, original), values-1)
            self.assertEqual(saved.call_count, 1)
            recorder.close('completed')

    def test_cached_frozen_file_or_path_change_cannot_advance_a_cycle(self):
        import os
        for kind in ('bytes_with_restored_mtime', 'replaced_inode', 'symlink'):
            with tempfile.TemporaryDirectory() as directory, self.subTest(kind=kind):
                recorder, values = self.fixture(directory)
                reference = recorder.index['initialSnapshot']['truth']['streams']['nodes']
                path = Path(directory)/reference['ref']
                previous = path.stat()
                raw = path.read_bytes()
                if kind == 'bytes_with_restored_mtime':
                    changed = bytearray(raw); changed[-1] ^= 1; path.write_bytes(changed)
                else:
                    path.unlink()
                    if kind == 'replaced_inode':
                        path.write_bytes(raw)
                    else:
                        other = Path(directory)/'outside-array.npy'; other.write_bytes(raw)
                        path.symlink_to(other)
                if kind != 'symlink':
                    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
                with self.assertRaisesRegex(ValueError, 'array|immutable'):
                    self.commit(recorder, .02, values)
                self.assertTrue(recorder.failed)
                self.assertEqual((recorder.cycles, recorder.index['committedBytes']), (0, 0))
                self.assertEqual(recorder.committed_observation['streams']['nodes'], reference)
                recorder.close('interrupted')

    def test_array_mappings_follow_the_rolling_working_set_instead_of_episode_length(self):
        import gc
        import numpy as np
        import weakref
        from unittest.mock import patch
        from runtime import records
        with tempfile.TemporaryDirectory() as directory:
            recorder, values = self.fixture(directory)
            mappings, peaks = [], []
            original_load = np.load
            def tracked_load(*args, **kwargs):
                result = original_load(*args, **kwargs)
                mappings.append(weakref.ref(result))
                return result
            with patch.object(np, 'load', side_effect=tracked_load):
                for cycle in range(1, 25):
                    self.commit(recorder, cycle*.02, values+cycle)
                    gc.collect()
                    peaks.append(sum(value() is not None for value in mappings))
            self.assertLessEqual(max(peaks), 2)
            recorder.close('completed')
            gc.collect()
            self.assertFalse(any(value() is not None for value in mappings))
            mappings.clear(); peaks.clear()
            original_validate = records._validate_cycle
            def tracked_validate(*args, **kwargs):
                result = original_validate(*args, **kwargs)
                gc.collect()
                peaks.append(sum(value() is not None for value in mappings))
                return result
            with patch.object(np, 'load', side_effect=tracked_load), \
                 patch.object(records, '_validate_cycle', new=tracked_validate):
                rows, _, _ = records.read_transitions(directory)
            self.assertEqual(len(rows), 24)
            self.assertLessEqual(max(peaks), 3)
            gc.collect()
            self.assertFalse(any(value() is not None for value in mappings))

    def test_one_scan_reuses_unchanged_array_but_still_rejects_between_cycle_mutation(self):
        from runtime import records
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            recorder, values = self.fixture(directory)
            for cycle in range(1, 4):self.commit(recorder, cycle*.02, values)
            recorder.close('completed')
            with patch.object(records, '_file_digest', wraps=records._file_digest) as digested:
                records.read_transitions(directory)
            self.assertEqual(digested.call_count, 1)
            original_validate = records._validate_cycle
            def mutate_after_first(row, *args, **kwargs):
                result = original_validate(row, *args, **kwargs)
                if row['cycleId'] == 0:
                    path = Path(directory)/row['truthAfter']['streams']['nodes']['ref']
                    raw = bytearray(path.read_bytes()); raw[-1] ^= 1; path.write_bytes(raw)
                return result
            with patch.object(records, '_validate_cycle', side_effect=mutate_after_first), \
                 self.assertRaisesRegex(ValueError, 'identity|digest'):
                records.read_transitions(directory)

    def test_consumers_reuse_one_checked_prefix_and_preserve_io_and_physical_tail_checks(self):
        from runtime import records
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            recorder, values = self.fixture(directory)
            for cycle in range(1, 3):self.commit(recorder, cycle*.02, values)
            recorder.close('completed')
            with (Path(directory)/'transitions.jsonl').open('ab') as file:file.write(b'uncommitted diagnostic tail')
            checked = records.read_transitions(directory)
            unchanged = deepcopy(checked)
            wrong_io = deepcopy(recorder.io); wrong_io['ports']['arm']['units'] = ['m']*8
            with patch.object(records, 'read_transitions', side_effect=AssertionError('repeated prefix scan')):
                batch = records.load_batch(directory, io=recorder.io, _transitions=checked)
                counts = records.execution_counts(directory, 25, _transitions=checked)
                with self.assertRaisesRegex(ValueError, 'frozen|contract'):
                    records.load_batch(directory, io=wrong_io, _transitions=checked)
                with self.assertRaisesRegex(ValueError, 'smaller'):
                    records.execution_counts(directory, 5, _transitions=checked)
            self.assertEqual((batch['controlCycles'], batch['uncommittedTail']), (2, True))
            self.assertEqual((counts['committedPhysicalSteps'], counts['uncommittedPhysicalSteps']), (20, 5))
            self.assertTrue(counts['uncommittedRecordTail'])
            self.assertEqual(checked, unchanged)


def diagnostic_recorder(directory, *args, frame=None, **kwargs):
    recorder = Recorder(directory, *args, **kwargs, io=BASE_IO)
    recorder.begin({'observation': observation(0, frame), 'truth': deepcopy(observation(0, frame))}, RubricFixture())
    return recorder


class RecordsTests(unittest.TestCase):
    def test_historical_v3_uses_its_original_reader_without_promoting_old_records(self):
        import hashlib
        import importlib.util
        root = Path(__file__).parent/'fixtures'
        source = root/'legacy-kernel/records_v3.py'
        if not (root/'old-v3'/'transitions.jsonl').is_file():
            self.skipTest('archived private v3 capture is not part of the source distribution')
        files = [source, *(root/'old-v3').iterdir()]
        before = {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        spec = importlib.util.spec_from_file_location('test_original_record_reader',source)
        original = importlib.util.module_from_spec(spec); spec.loader.exec_module(original)
        batch = original.load_batch(root/'old-v3')
        self.assertEqual(batch['actions'].shape,(40,8)); self.assertEqual(batch['observations'].shape,(40,18))
        with self.assertRaisesRegex(ValueError,'protocol'): load_batch(root/'old-v3')
        self.assertEqual(before,{str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in files})
    def test_failed_index_publication_keeps_the_previous_commit_on_close(self):
        from copy import deepcopy
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            recorder = diagnostic_recorder(path)
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            committed = deepcopy(recorder.index)
            error = OSError('synthetic index publication failure')
            try:
                with patch('runtime.records.atomic_json', side_effect=error):
                    with self.assertRaises(OSError) as caught:
                        recorder.commit(observation(.02), COMMAND, COMMAND, observation(.04), 10, truth_before=deepcopy(observation(.02)), truth_after=deepcopy(observation(.04)))
                self.assertIs(caught.exception, error)
                self.assertEqual((recorder.cycles, recorder.physical_steps), (1, 10))
                self.assertEqual(recorder.index, committed)
                with self.assertRaisesRegex(RuntimeError, 'failed'):
                    recorder.commit(observation(.02), COMMAND, COMMAND, observation(.04), 10, truth_before=deepcopy(observation(.02)), truth_after=deepcopy(observation(.04)))
                recorder.close('success')
                saved = json.loads((path / 'records.json').read_text())
                self.assertEqual(saved['committedBytes'], committed['committedBytes'])
                self.assertEqual(saved['committedCycles'], 1)
                self.assertFalse(saved['terminated'])
                self.assertEqual(saved['status'], 'controller_error')
                self.assertEqual(load_batch(path)['controlCycles'], 1)
                self.assertTrue(load_batch(path)['uncommittedTail'])
            finally:
                recorder.file.close()

    def test_failed_data_fsync_does_not_allow_a_later_commit_or_success(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            recorder = diagnostic_recorder(path)
            error = OSError('synthetic data fsync failure')
            try:
                with patch('runtime.records.os.fsync', side_effect=error):
                    with self.assertRaises(OSError) as caught:
                        recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
                self.assertIs(caught.exception, error)
                self.assertEqual((recorder.cycles, recorder.physical_steps), (0, 0))
                with self.assertRaisesRegex(RuntimeError, 'failed'):
                    recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
                recorder.close('success')
                saved = json.loads((path / 'records.json').read_text())
                self.assertEqual(saved['committedBytes'], 0)
                self.assertEqual(saved['committedCycles'], 0)
                self.assertFalse(saved['terminated'])
                from runtime.records import read_transitions
                rows, _, tail = read_transitions(path)
                self.assertEqual(rows, [])
                self.assertTrue(tail)
            finally:
                recorder.file.close()

    def test_close_publication_failure_closes_the_file_and_preserves_boundary(self):
        from copy import deepcopy
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            recorder = diagnostic_recorder(path)
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            committed = deepcopy(recorder.index)
            error = OSError('synthetic close publication failure')
            try:
                with patch('runtime.records.atomic_json', side_effect=error):
                    with self.assertRaises(OSError) as caught:
                        recorder.close('success')
                self.assertIs(caught.exception, error)
                self.assertTrue(recorder.file.closed)
                self.assertEqual(recorder.index, committed)
                self.assertEqual(json.loads((path / 'records.json').read_text()), committed)
                self.assertEqual(load_batch(path)['controlCycles'], 1)
            finally:
                recorder.file.close()

    def test_execution_counts_distinguish_a_physical_tail_from_committed_rows(self):
        from runtime.records import execution_counts
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            recorder = diagnostic_recorder(path)
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            recorder.close('controller_error')
            counts = execution_counts(path, 20)
            self.assertEqual(counts['committedPhysicalSteps'], 10)
            self.assertEqual(counts['totalExecutedPhysicalSteps'], 20)
            self.assertEqual(counts['uncommittedPhysicalSteps'], 10)
            self.assertFalse(counts['uncommittedRecordTail'])
            self.assertEqual(load_batch(path)['physicalSteps'], 10)
            with self.assertRaisesRegex(ValueError, 'smaller than'):
                execution_counts(path, 5)
            self.assertEqual(execution_counts(path, 10)['uncommittedPhysicalSteps'], 0)

    def test_elapsed_time_must_match_the_recorded_physics_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            recorder = diagnostic_recorder(path)
            after = observation(.04)
            after['physicsStep'] = 10
            with self.assertRaisesRegex(ValueError, 'physical clock'):
                recorder.commit(observation(0), COMMAND, COMMAND, after, 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(after))
            self.assertEqual(recorder.cycles, 0)
            recorder.close('interrupted')

    def test_legacy_protocol_and_gripper_units_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            recorder = diagnostic_recorder(path)
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            recorder.close('completed')
            index = json.loads((path / 'records.json').read_text())
            index['schema'] = 'sceneagent-control-transitions-v2'
            atomic_json(path / 'records.json', index)
            with self.assertRaisesRegex(ValueError, 'protocol'):
                load_batch(path)
        with tempfile.TemporaryDirectory() as directory:
            recorder = diagnostic_recorder(Path(directory))
            with self.assertRaisesRegex(ValueError, 'limits'):
                recorder.commit(observation(0), {'arm':HOLD[:7]+[255]}, {'arm':HOLD[:7]+[255]}, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            recorder.close('interrupted')
        with tempfile.TemporaryDirectory() as directory:
            recorder = diagnostic_recorder(Path(directory))
            with self.assertRaisesRegex(ValueError,'boolean|finite'):
                recorder.commit(observation(0),{'arm':[True,*HOLD[1:]]},COMMAND,observation(.02),10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            self.assertEqual(recorder.cycles,0)
            recorder.close('policy_error')

    def test_wrong_units_joint_order_and_physical_clocks_are_rejected(self):
        from copy import deepcopy
        from runtime.records import validate_publication
        for field,value in (('units',[]),('shape',[-1]),('limits',[[1,0]]*8)):
            io=deepcopy(BASE_IO);io['ports']['arm'][field]=value
            with tempfile.TemporaryDirectory() as directory,self.subTest(field=field),self.assertRaises(ValueError):
                Recorder(directory,io=io)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            recorder = diagnostic_recorder(path)
            before = observation(0)
            after = observation(.02)
            after['physicsStep'] = 11
            with self.assertRaisesRegex(ValueError, 'physical.*clock'):
                recorder.commit(before, COMMAND, COMMAND, after, 10, truth_before=deepcopy(before), truth_after=deepcopy(after))
            self.assertEqual(recorder.cycles, 0)
            recorder.close('interrupted')
            job = path / 'jobs/j1'
            job.mkdir(parents=True)
            atomic_json(job / 'status.json', {'state': 'running'})
            with self.assertRaisesRegex(ValueError, 'committed'):
                validate_publication({'publicationJobId': 'j1', 'status': 'completed'}, path)
            atomic_json(job / 'status.json', {'jobId': 'j1', 'state': 'completed'})
            manifest = {'publicationJobId': 'j1', 'status': 'completed', 'projectId': 'p1', 'revision': 'r1',
                        'artifacts': {'manifest': 'manifest.json'}}
            atomic_json(path / 'manifest.json', manifest)
            import hashlib
            receipt = {'projectId': 'p1', 'revision': 'r1', 'publicationArtifactDigests': {
                       'manifest.json': hashlib.sha256((path / 'manifest.json').read_bytes()).hexdigest()}}
            atomic_json(job / 'result.json', receipt)
            validate_publication(manifest, path)
            receipt['revision'] = 'wrong'
            atomic_json(job / 'result.json', receipt)
            with self.assertRaisesRegex(ValueError, 'another scene'):
                validate_publication(manifest, path)

    def test_concurrent_atomic_writers_use_distinct_temporary_files(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, Event, Thread
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'shared.json'
            atomic_json(destination, {'initial': True})
            barrier = Barrier(2)
            stop, reading = Event(), Event()
            reader_errors = []
            def read_while_writers_publish():
                while not stop.is_set():
                    try:
                        json.loads(destination.read_text())
                        reading.set()
                    except Exception as exc:
                        reader_errors.append(exc)
            reader = Thread(target=read_while_writers_publish)
            reader.start()
            self.assertTrue(reading.wait(timeout=2))
            original = Path.replace
            def simultaneous_replace(temporary, target):
                barrier.wait(timeout=5)
                return original(temporary, target)
            with patch.object(Path, 'replace', simultaneous_replace), ThreadPoolExecutor(2) as executor:
                futures = [executor.submit(atomic_json, destination, {'writer': i, 'payload': str(i) * 4096})
                           for i in range(2)]
                errors = [future.exception() for future in futures if future.exception()]
            stop.set()
            reader.join(timeout=2)
            self.assertEqual(errors, [])
            self.assertEqual(reader_errors, [])
            saved = json.loads(destination.read_text())
            self.assertEqual(saved['payload'], str(saved['writer']) * 4096)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_every_control_action_has_before_after_and_separate_camera_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = diagnostic_recorder(Path(directory))
            for tick in range(3):
                recorder.commit(observation(tick * .02), COMMAND, COMMAND,
                                observation((tick + 1) * .02), 10, truth_before=deepcopy(observation(tick * .02)), truth_after=deepcopy(observation((tick + 1) * .02)))
            recorder.close('completed')
            batch = load_batch(Path(directory))
            self.assertEqual((len(batch['actions']),len(batch['actions'][0]['arm'])), (3, 8))
            self.assertEqual((len(batch['observations']),len(batch['observations'][0]['streams']['q'])+len(batch['observations'][0]['streams']['dq'])), (3, 18))
            self.assertEqual(batch['physicalSteps'], 30)
            self.assertEqual(batch['controlCycles'], 3)

    def test_partial_tail_is_preserved_and_never_read_as_a_committed_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            recorder = diagnostic_recorder(path)
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            recorder.close('interrupted')
            with (path / 'transitions.jsonl').open('a') as file:
                file.write('{"cycleId":1')
            batch = load_batch(path)
            self.assertEqual(batch['controlCycles'], 1)
            self.assertTrue(batch['uncommittedTail'])
            self.assertTrue((path / 'transitions.jsonl').read_text().endswith('{"cycleId":1'))

    def test_loader_rejects_post_action_frame_invalid_action_and_empty(self):
        # SYSTEM_VERIFICATION_ONLY v5 sensor fixture, never a simulator sample.
        camera = {'cameraId':'front', 'frame':'world', 'at':[0,0,1], 'orientation':[1,0,0,0],
                  'resolution':[320,240], 'rateHz':25, 'modalities':['rgb'], 'video':False,
                  'required':True, 'source':'simulation_observer'}
        frame = {'index':0, 'cameraId':'front', 'frameIndex':0, 'sampleId':10, 'physicsStep':10,
                 'time':.02, 'simulationTime':1.02, 'timeOrigin':1, 'stageId':'stage1', 'path':'frames/front/000000.png',
                 'sha256':'a'*64, 'worldFromCamera':[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
                 'intrinsics':{'fx':200,'fy':200,'cx':160,'cy':120}}
        def observed(time):
            value = observation(time)
            value['cameraFrames'] = {'front':frame}
            return value
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            recorder = Recorder(path, recording={'cameras':[camera], 'primaryCameraId':'front'}, io=BASE_IO)
            recorder.begin({'observation':observed(0), 'truth':deepcopy(observed(0))}, RubricFixture())
            recorder.commit(observed(0), COMMAND, COMMAND, observed(.02), 10, truth_before=deepcopy(observed(0)), truth_after=deepcopy(observed(.02)))
            recorder.close('completed')
            with self.assertRaisesRegex(ValueError, 'future'):
                load_batch(path)
        with tempfile.TemporaryDirectory() as directory:
            diagnostic_recorder(Path(directory)).close('completed')
            with self.assertRaisesRegex(ValueError, 'empty'):
                load_batch(Path(directory))



def _v5_world():
    world = World(); world.box('cube', [.05,.05,.05]); return world


def _v5_recording():
    return {'primaryCameraId': 'front', 'cameras': [{
        'cameraId': 'front', 'frame': 'world', 'at': [1., 0., 1.],
        'orientation': [1., 0., 0., 0.], 'resolution': [320, 240],
        'rateHz': 25, 'modalities': ['rgb'], 'video': True,
        'required': True, 'source': 'simulation_observer'}]}


def _v5_task(**fields):
    return Task('numeric_fixture', bindings={'subject': 'cube'},
                rubric={'sourceRef': 'rubrics.py', 'helperRefs': []}, **fields)


def _v5_frame(*, time=0, index=0, frame_index=0, camera_id='front'):
    return {'cameraId': camera_id, 'index': index, 'frameIndex': frame_index,
            'sampleId': round(time*500), 'physicsStep': round(time*500),
            'time': time, 'simulationTime': time+1, 'timeOrigin': 1, 'stageId': 'stage1',
            'path': f'frames/{camera_id}/{frame_index:06d}.png', 'sha256': 'b'*64,
            'worldFromCamera': [[1.,0.,0.,0.], [0.,1.,0.,0.], [0.,0.,1.,0.], [0.,0.,0.,1.]],
            'intrinsics': {'fx': 200., 'fy': 200., 'cx': 160., 'cy': 120.}}


def _v5_camera_observation(time, frame_value=None):
    value = observation(time)
    value.pop('cameraFrame', None)
    value['cameraFrames'] = {'front': frame_value or _v5_frame()}
    return value


class RecordSemanticsContracts(unittest.TestCase):
    def test_camera_roundoff_tolerance_does_not_relax_the_control_clock(self):
        from runtime.records import validate_camera_frames
        validate_camera_frames(_v5_camera_observation(0, _v5_frame(time=5e-8)), _v5_recording(), physics_rate_hz=500)
        with self.assertRaisesRegex(ValueError, 'future|clock'):
            validate_camera_frames(_v5_camera_observation(0, _v5_frame(time=2e-7)), _v5_recording(), physics_rate_hz=500)
        with tempfile.TemporaryDirectory() as directory:
            recorder = diagnostic_recorder(Path(directory))
            with self.assertRaisesRegex(ValueError, 'physical clock'):
                recorder.commit(observation(0), COMMAND, COMMAND, observation(.02+5e-8), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02+5e-8)))
            self.assertEqual(recorder.cycles, 0)
            recorder.close('interrupted')

    def test_uncommitted_annotation_tail_is_not_exported(self):
        from unittest.mock import patch
        class AnnotationFixture(RubricFixture):
            def preview(self, phase, facts):
                value = super().preview(phase,facts)
                if phase == 'transition':
                    value['result']['details'] = {'annotations':[{'id':'phase_'+str(facts['cycleId']),
                        'text':'Generated committed phase', 'bindings':['subject']}]}
                return value
        with tempfile.TemporaryDirectory() as directory:
            declaration = _v5_task().bind(_v5_world())
            recorder = Recorder(directory, task=declaration, io=BASE_IO)
            recorder.begin({'observation':observation(0), 'truth':deepcopy(observation(0))}, AnnotationFixture())
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            with patch('runtime.records.atomic_json',side_effect=OSError('CPU fixture publication failure')):
                with self.assertRaises(OSError):recorder.commit(observation(.02), COMMAND, COMMAND, observation(.04), 10, truth_before=deepcopy(observation(.02)), truth_after=deepcopy(observation(.04)))
            recorder.close('interrupted')
            batch = load_batch(directory)
            self.assertTrue(batch['uncommittedTail'])
            self.assertEqual([a['id'] for a in batch['semanticAnnotations']],['phase_0'])
            self.assertEqual(batch['semanticAnnotations'][0]['cycleEnd'],1)

    def test_data_reader_uses_declared_dimensions_without_loosening_native_writes(self):
        # SYSTEM_VERIFICATION_ONLY: dimensional recording does not qualify a robot SDK.
        io = deepcopy(BASE_IO)
        io['ports']['arm'].update(shape=[7], units=['rad']*7, limits=[[-4,4]]*7)
        io['ports']['arm']['source']['joints']=[f'fixture_joint_{i}' for i in range(7)]
        for name in ('q','dq'):
            io['streams'][name].update(shape=[8],units=['rad' if name=='q' else 'rad/s']*8)
            io['streams'][name]['source']['joints']=[f'fixture_joint_{i}' for i in range(8)]
        def observed(t):
            value=observation(t);value['streams']={'q':[0]*8,'dq':[0]*8};return value
        with tempfile.TemporaryDirectory() as directory:
            recorder=Recorder(directory,io=io);session=RubricFixture()
            recorder.begin({'observation':observed(0),'truth':deepcopy(observed(0))},session)
            command={'arm':HOLD[:6]+HOLD[-1:]}
            recorder.commit(observed(0),command,command,observed(.02),10,
                truth_before=deepcopy(observed(0)),truth_after=deepcopy(observed(.02)))
            recorder.close('completed');batch=load_batch(directory,io=io)
            self.assertEqual(len(batch['actions'][0]['arm']),7)
            self.assertEqual(len(batch['observations'][0]['streams']['q']),8)
            with self.assertRaisesRegex(ValueError,'frozen'):load_batch(directory,io=BASE_IO)

    def test_v5_preserves_generated_annotations_without_promoting_them_to_success(self):
        class AnnotationFixture(RubricFixture):
            def preview(self, phase, facts):
                value = super().preview(phase, facts)
                if phase == 'transition':
                    value['result']['details'] = {'annotations': [
                        {'id': 'phase_a', 'text': 'Generated numeric phase', 'bindings': ['subject']}]}
                return value
        with tempfile.TemporaryDirectory() as directory:
            declaration = _v5_task().bind(_v5_world())
            recorder = Recorder(directory, task=declaration, io=BASE_IO)
            recorder.begin({'observation': observation(0), 'truth': deepcopy(observation(0))}, AnnotationFixture())
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            recorder.close('completed')
            batch = load_batch(directory)
            self.assertFalse(batch['lastResult']['success'])
            self.assertEqual(batch['semanticAnnotations'][0]['cycleStart'], 0)
            self.assertEqual(batch['semanticAnnotations'][0]['cycleEnd'], 1)
            self.assertEqual(batch['semanticAnnotations'][0]['nodeIds'], ['cube'])
            self.assertEqual(batch['semanticAnnotations'][0]['kind'], 'generated_interpretation')
            self.assertNotIn('rubricState', batch['semanticAnnotations'][0])

    def test_future_wrong_stage_or_nonrigid_calibration_is_rejected(self):
        from runtime.records import validate_camera_frames
        for kind in ('future', 'stage', 'calibration', 'identity', 'path', 'sample'):
            sample = _v5_frame()
            if kind == 'future': sample = _v5_frame(time=.02)
            elif kind == 'stage': sample['stageId'] = 'other'
            elif kind == 'calibration': sample['worldFromCamera'][0][0] = 2.
            elif kind == 'identity': sample['cameraId'] = 'other'
            elif kind == 'path': sample['path'] = '../private.png'
            else: sample['sampleId'] = 20
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_camera_frames(_v5_camera_observation(0, sample), _v5_recording(), physics_rate_hz=500)

    def test_hidden_required_view_is_not_a_required_policy_sensor(self):
        declaration = _v5_task(recording=_v5_recording()).bind(_v5_world())
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory, recording=_v5_recording(), task=declaration, io=BASE_IO)
            recorder.begin({'observation': observation(0), 'truth': deepcopy(observation(0))}, RubricFixture())
            recorder.commit(observation(0), COMMAND, COMMAND, observation(.02), 10, truth_before=deepcopy(observation(0)), truth_after=deepcopy(observation(.02)))
            recorder.close('completed')
            self.assertEqual(load_batch(directory)['controlCycles'], 1)
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory, recording=_v5_recording(), task=declaration, io=BASE_IO)
            recorder.begin({'observation': _v5_camera_observation(0), 'truth': deepcopy(_v5_camera_observation(0))}, RubricFixture())
            recorder.commit(_v5_camera_observation(0), COMMAND, COMMAND, _v5_camera_observation(.02), 10, truth_before=deepcopy(_v5_camera_observation(0)), truth_after=deepcopy(_v5_camera_observation(.02)))
            recorder.close('completed')
            with self.assertRaisesRegex(ValueError, 'unauthorized'):
                load_batch(directory)

    def test_annotation_outside_frozen_bindings_does_not_become_valid_semantics(self):
        from runtime.records import project_annotations
        source = {'cycleId': 0, 'timeStart':0, 'timeEnd':.02, 'factsDigest':'a'*64,
                  'rubricResult': {'details': {'annotations':[
                      {'id':'phase', 'text':'Generated phase', 'bindings':['missing']}]}}}
        with self.assertRaisesRegex(ValueError, 'binding'):
            project_annotations([source], _v5_task().bind(_v5_world()), RubricFixture.descriptor)



if __name__ == '__main__':
    unittest.main()
