"""SYSTEM_VERIFICATION_ONLY CPU regressions; no SDK or generated product acceptance."""
from collections import Counter
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtime import jobs, records, worker


class ConsumerVerificationTests(unittest.TestCase):
    def test_each_case_is_verified_once_per_parent_pass_with_the_same_required_closure(self):
        from tests.test_frozen_evaluation import FrozenEvaluationTests
        fixture = FrozenEvaluationTests()
        self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            project, directory, manifest, world = fixture.context(root)
            cases = []
            for source in ('case_two.py', 'case_zero.py'):
                payload = fixture.payload(operation='rollout', root=str(root), purpose='verification',
                    controller={'type':'policy', 'policyRef':source, 'digest':manifest['sourceDigests'][source]})
                value = worker.rollout(payload, root)
                worker.finalize({**payload, 'jobId':'published_case'}, value)
                cases.append(value['episode'])
            payload = fixture.payload(operation='rollout', root=str(root), jobId='published_expert')
            value = worker.rollout(payload, root)
            worker.finalize(payload, value)
            original = worker.run_controller
            def controller(*args, **kwargs):
                if kwargs.get('kind') == 'isolation':
                    return {'isolationChecks': {**{name:True for name,_,_ in kwargs['probes']},'networkBlocked':True}}
                return original(*args, **kwargs)
            payload = fixture.payload(operation='consume', root=str(root), episodes=[{'episodeId':value['episode'],'taskId':'events','use':'successful_demonstration'}],
                                      validationEpisodes=cases, requestId='cpu_consumer')
            with patch.object(worker, 'run_controller', side_effect=controller):
                result = worker.consume_internal(payload, root)
            with patch.object(worker, 'verify_episode', wraps=worker.verify_episode) as verify:
                required = worker.required_files(payload, result)
            counts = Counter(Path(call.args[1]).name for call in verify.call_args_list)
            self.assertEqual({id:counts[id] for id in cases}, dict.fromkeys(cases, 1))
            for id in [*cases, value['episode']]:
                for filename in ('manifest.json', 'records.json', 'transitions.jsonl', 'annotations.json'):
                    self.assertIn(project/'episodes'/id/filename, required)
            # A later parent pass reads the files again; no request/global memo may authorize changed bytes.
            path = directory/'rubrics.py'
            path.write_text(path.read_text()+'\n# changed after the earlier child/pass\n')
            with self.assertRaisesRegex(ValueError, 'source digest'):
                worker.required_files(payload, result)


class ArrayPathTests(unittest.TestCase):
    def test_array_scope_checks_do_not_scan_absolute_ancestors(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            for depth in (1, 18):
                directory = root.joinpath(*(['nested']*depth))
                directory.mkdir(parents=True, exist_ok=True)
                reference = records._array_reference(directory, np.arange(168*3, dtype=np.float32).reshape(168, 3))
                original = Path.is_relative_to
                with patch.object(Path, 'is_relative_to', autospec=True,
                                  side_effect=lambda path, parent:original(path, parent)) as comparisons:
                    restored = records.read_array(directory, reference)
                self.assertEqual(restored.shape, (168, 3))
                self.assertLessEqual(comparisons.call_count, 3, 'a short ref must not compare every absolute ancestor')

    def test_relative_walk_keeps_parent_and_leaf_symlinks_and_digest_checks(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            reference = records._array_reference(root, np.arange(504, dtype=np.float32).reshape(168, 3))
            target = root/reference['ref']
            original = target.read_bytes()
            for invalid in ('../'+reference['ref'], '/'+reference['ref']):
                with self.subTest(ref=invalid), self.assertRaisesRegex(ValueError, 'scope'):
                    records.read_array(root, dict(reference, ref=invalid))
            target.write_bytes(original[:-1]+b'X')
            with self.assertRaisesRegex(ValueError, 'digest'):
                records.read_array(root, reference)
            target.write_bytes(original)
            copy = root/'copy.npy';copy.write_bytes(original)
            target.unlink();target.symlink_to(copy)
            with self.assertRaisesRegex(ValueError, 'scope'):
                records.read_array(root, reference)
            target.unlink();target.write_bytes(original)
            (root/'arrays').rename(root/'real-arrays');(root/'arrays').symlink_to(root/'real-arrays')
            with self.assertRaisesRegex(ValueError, 'scope'):
                records.read_array(root, reference)


class PhaseDiagnosticsTests(unittest.TestCase):
    def invoke(self, directory, fail=False):
        from runtime import simulation
        payload = {'root':str(directory), 'operation':'consume'}
        (directory/'worker-parent.json').write_text(json.dumps({'pid':os.getppid(), 'processToken':jobs._token(os.getppid())}))
        (directory/'worker-request.json').write_text(json.dumps(payload))
        observed = []
        def action(phase, value=None):
            def invoke(*args, **kwargs):
                prefix = json.loads((directory/'performance.json').read_text())
                observed.append(prefix['phase'])
                self.assertEqual(prefix['phase'], phase)
                if phase == 'run' and fail:
                    raise RuntimeError('original CPU business failure')
                return value
            return invoke
        def shutdown(code):
            jobs.shutdown_started(False)
            raise SystemExit(code)
        with patch.object(worker, 'JOB_DIRECTORY', None), patch.object(sys, 'platform', 'cpu-fixture'), \
             patch.object(simulation, '_ensure_app', side_effect=action('sdk_initialization')), \
             patch.object(worker, 'run', side_effect=action('run', {'passed':True})), \
             patch.object(simulation, 'finish', side_effect=action('finish', {})), \
             patch.object(worker, 'required_files', side_effect=action('required_files', set())), \
             patch.object(jobs, 'write_candidate', side_effect=action('candidate_write')), \
             patch.object(simulation, 'shutdown', side_effect=shutdown), self.assertRaises(SystemExit) as ended:
            worker.kit_entry(directory)
        return ended.exception.code, observed

    def test_worker_persists_begin_before_each_long_boundary(self):
        with tempfile.TemporaryDirectory() as name:
            code, observed = self.invoke(Path(name))
            self.assertEqual(code, 0)
            self.assertEqual(observed, ['sdk_initialization','run','finish','required_files','candidate_write'])

    def test_failure_diagnostic_write_does_not_hide_the_original_business_error(self):
        with tempfile.TemporaryDirectory() as name:
            original = worker.save
            def save(path, value):
                if Path(path).name == 'worker-failure.json':
                    raise OSError('CPU diagnostic storage unavailable')
                return original(path, value)
            stderr = io.StringIO()
            with patch.object(worker, 'save', side_effect=save), redirect_stderr(stderr):
                code, observed = self.invoke(Path(name), fail=True)
            self.assertEqual(code, 7)
            self.assertIn('original CPU business failure', stderr.getvalue())
            self.assertIn('CPU diagnostic storage unavailable', stderr.getvalue())

    def test_invalid_progress_is_only_a_diagnostic_failure_and_next_phase_clears_it(self):
        with tempfile.TemporaryDirectory() as name, jobs.performance_context(Path(name), {'operation':'check'}):
            with self.assertRaisesRegex(RuntimeError, 'CPU business error'):
                jobs.flush_performance('invalid', progress={'completed':True, 'total':1})
                raise RuntimeError('CPU business error')
            jobs.flush_performance('next_boundary')
            prefix = json.loads((Path(name)/'performance.json').read_text())
            self.assertEqual(prefix['phase'], 'next_boundary')
            self.assertTrue(prefix['diagnosticFailed'])
            self.assertNotIn('progress', prefix)

    @unittest.skipUnless(sys.platform == 'linux', 'actual timeout/reap requires Linux process identities')
    def test_real_timeout_keeps_matching_phase_and_progress_but_rejects_foreign_prefix(self):
        for mode in ('matching', 'foreign', 'malformed'):
            with tempfile.TemporaryDirectory() as name, self.subTest(mode=mode):
                root = Path(name);work = root/'work';script = root/'child.py'
                script.write_text("""import json,pathlib,sys,time
sys.path.insert(0,sys.argv[1])
from runtime import jobs
work=pathlib.Path(sys.argv[2]);payload=json.loads((work/'worker-request.json').read_text())
jobs.start_performance(work,payload)
if sys.argv[3]=='foreign':jobs._timing['document']['worker']['processToken']='foreign-token'
jobs.flush_performance('consume:validation', progress={'episodeId':'e_fixture','completed':1,'total':3})
if sys.argv[3]=='malformed':(work/'performance.json').write_text('[]')
time.sleep(30)
""")
                payload = {'root':str(root), 'operation':'check'}
                with self.assertRaisesRegex(TimeoutError, 'wall-clock budget exceeded') as caught:
                    jobs.supervise(payload, lambda *_: [], work,
                        [sys.executable,str(script),str(Path(jobs.__file__).parent.parent),str(work),mode], timeout=1)
                notes = '\n'.join(getattr(caught.exception, '__notes__', []))
                self.assertIn('consume:validation' if mode == 'matching' else 'unavailable', notes)
                self.assertIn('consume:validation' if mode == 'matching' else 'unavailable', str(caught.exception))
                if mode == 'matching':
                    self.assertIn('e_fixture', notes)
                else:
                    self.assertNotIn('consume:validation', notes)
                identity = json.loads((work/'worker-process.json').read_text())
                self.assertIsNone(jobs._token(identity['pid']))
                self.assertEqual(jobs._group_members(identity['pid']), {})
                self.assertFalse((work/'candidate.json').exists())


if __name__ == '__main__':
    unittest.main()
