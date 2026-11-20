"""Malformed diagnostic prefixes in isolated real CPU processes; no SDK is started."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from runtime import jobs
from runtime.records import atomic_json
from tests import test_performance as performance_fixtures


class FinalizeError(RuntimeError):
    pass


class DiagnosticIsolationTests(unittest.TestCase):
    def fixture(self):
        fixture = performance_fixtures.PerformanceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def child(self, fixture, changes):
        report = fixture.root / 'report.json'
        report.write_text('{"passed":true}')
        script = fixture.root / 'diagnostic-child.py'
        script.write_text("""import json,pathlib,sys
sys.path.insert(0,sys.argv[1])
from runtime import jobs
work=pathlib.Path(sys.argv[2]);payload=json.loads((work/'worker-request.json').read_text())
jobs.start_performance(work,payload)
with jobs.measure('prepare'): pass
jobs.write_candidate(work,payload,{'passed':True},[pathlib.Path(payload['root'])/'report.json'],dict.fromkeys(('records','images','encoder','replicator'),True))
document=jobs._timing['document'];document.update(json.loads(sys.argv[3]));document['phase']='shutdown_started'
# Intentional diagnostic corruption only; the candidate and business exit remain valid.
(work/'performance.json').write_text(json.dumps(document))
""")
        return lambda payload: jobs.supervise(payload, lambda *_: [report], fixture.job,
            [sys.executable, str(script), str(Path(jobs.__file__).parent.parent), str(fixture.job), json.dumps(changes)])

    def test_actual_native_build_group_names_survive_terminal_diagnostics(self):
        fixture = self.fixture()
        groups = {name: {'seconds': .01, 'count': 1, 'failedCount': 0}
                  for name in ('sourceBuild', 'nativeAudit', 'nativeBinding')}
        atomic_json(fixture.job / 'performance.json', {**fixture.prefix, 'groups': groups})
        parent = jobs._parent_performance(fixture.job, fixture.payload, 'worker_verification_only')
        parent['counters'].update(workerExitCode=0,
            verification={'seconds': .02, 'count': 1, 'failedCount': 0, 'state': 'completed'})
        jobs._write_performance(fixture.job, parent)
        self.assertEqual(fixture.performance()['groups'], groups)
        self.assertFalse(fixture.performance()['incomplete'])
        self.assertFalse(fixture.performance().get('diagnosticFailed', False))

    def test_new_printable_group_needs_no_second_name_registration(self):
        fixture = self.fixture()
        groups = {'future native measurement / v2': {'seconds': .03, 'count': 2, 'failedCount': 0}}
        atomic_json(fixture.job / 'performance.json', {**fixture.prefix, 'groups': groups})
        jobs._write_performance(fixture.job)
        self.assertEqual(fixture.performance()['groups'], groups)
        self.assertFalse(fixture.performance().get('diagnosticFailed', False))

    def test_group_name_and_counter_validation_stays_strict_without_a_name_list(self):
        valid = {'seconds': .01, 'count': 1, 'failedCount': 0}
        invalid = [(name, valid) for name in ('', '   ', 'x'*129, 'line\nbreak', 'tab\tname', 'control\x7f')]
        invalid += [('future timing', row) for row in (
            {'seconds': float('inf'), 'count': 1, 'failedCount': 0},
            {'seconds': float('nan'), 'count': 1, 'failedCount': 0},
            {'seconds': .01, 'count': True, 'failedCount': 0},
            {'seconds': .01, 'count': 1, 'failedCount': 2})]
        for name, row in invalid:
            with self.subTest(name=repr(name), row=repr(row)):
                fixture = self.fixture()
                (fixture.job / 'performance.json').write_text(json.dumps({**fixture.prefix, 'groups': {name: row}}))
                jobs._write_performance(fixture.job)
                self.assertTrue(fixture.performance()['incomplete'])
                self.assertTrue(fixture.performance()['diagnosticFailed'])
                self.assertEqual(fixture.performance()['groups'], {})

    def test_bad_shutdown_counter_cannot_replace_success_or_skip_publication(self):
        for counter in (10**400, float('inf'), float('nan'), 'broken', True, [], -1):
            with self.subTest(counter=repr(counter)):
                fixture = self.fixture()
                finalizer = Mock(side_effect=lambda payload, result: result)
                with patch.object(jobs, 'verify_candidate', wraps=jobs.verify_candidate) as verify:
                    jobs.execute(fixture.job, self.child(fixture, {'shutdown': {'state': 'started', 'startedCounter': counter}}), finalizer)
                self.assertEqual(verify.call_count, 1)
                self.assertEqual(verify.call_args.args[2], 0)
                self.assertEqual(json.loads((fixture.job / 'status.json').read_text())['state'], 'completed')
                finalizer.assert_called_once()
                self.assertTrue(fixture.performance()['incomplete'])
                self.assertEqual(fixture.performance()['parent']['publication']['state'], 'completed')
                self.assertEqual(fixture.performance()['parent']['workerExitCode'], 0)

    @unittest.skipUnless(sys.platform == 'linux', 'real process identity checks require Linux')
    def test_bad_parent_rows_do_not_escape_cancel_or_dead_supervisor_recovery(self):
        corruptions = ('broken', {'seconds': 10**400, 'count': 1, 'failedCount': 0},
                       {'seconds': float('nan'), 'count': 1, 'failedCount': 0},
                       {'seconds': .1, 'count': True, 'failedCount': 0},
                       {'seconds': .1, 'count': 1, 'failedCount': 2}, [])
        for mode in ('cancel', 'death'):
            for row in corruptions:
                with self.subTest(mode=mode, row=repr(row)):
                    fixture = self.fixture()
                    supervisor = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'], start_new_session=True)
                    supervisor_identity = {'pid': supervisor.pid, 'processToken': jobs._token(supervisor.pid)}
                    self.addCleanup(supervisor.wait)
                    self.addCleanup(jobs.stop_process_group, supervisor_identity, supervisor)
                    atomic_json(fixture.job / 'status.json', {'jobId': 'j1', 'state': 'running', **supervisor_identity})
                    script = fixture.root / 'alive-worker.py'
                    script.write_text("""import json,pathlib,sys,time
sys.path.insert(0,sys.argv[1])
from runtime import jobs
work=pathlib.Path(sys.argv[2]);payload=json.loads((work/'worker-request.json').read_text())
jobs.start_performance(work,payload)
with jobs.measure('prepare'): pass
document=jobs._timing['document'];document['parent']={'workerExitCode':0,'diagnosticFlush':json.loads(sys.argv[3])}
(work/'performance.json').write_text(json.dumps(document))
(work/'diagnostic-fixture-ready').touch()
time.sleep(30)
""")
                    (fixture.job / 'performance.json').unlink()
                    worker = subprocess.Popen([sys.executable, str(script), str(Path(jobs.__file__).parent.parent), str(fixture.job), json.dumps(row)], start_new_session=True)
                    identity = {'pid': worker.pid, 'processToken': jobs._token(worker.pid)}
                    self.addCleanup(worker.wait)
                    self.addCleanup(jobs.stop_process_group, identity, worker)
                    atomic_json(fixture.job / 'worker-process.json', identity)
                    deadline = time.monotonic() + 5
                    while not (fixture.job / 'diagnostic-fixture-ready').exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue((fixture.job / 'diagnostic-fixture-ready').exists())
                    if mode == 'death':
                        supervisor.kill()
                        supervisor.wait()
                    result = jobs.inspect(fixture.root, 'j1', 'cancel' if mode == 'cancel' else 'status')
                    self.assertEqual(result['state'], 'cancelled' if mode == 'cancel' else 'failed')
                    self.assertIsNone(jobs._token(worker.pid))
                    self.assertIsNone(jobs._token(supervisor.pid))
                    self.assertTrue(fixture.performance()['incomplete'])
                    self.assertIsInstance(fixture.performance()['parent'].get('diagnosticFlush', {}), dict)
                    self.assertEqual(fixture.performance()['parent']['publication'], {'state': 'not_executed'})

    def test_real_finalize_error_is_preserved_with_corrupt_diagnostics(self):
        for changes in ({'shutdown': {'state': 'started', 'startedCounter': 10**400}},
                        {'shutdown': {'state': 'not_applicable'}, 'parent': {'workerExitCode': 0, 'diagnosticFlush': 'broken'}}):
            with self.subTest(changes=repr(changes)):
                fixture = self.fixture()
                finalizer = Mock(side_effect=FinalizeError('original publication failure'))
                jobs.execute(fixture.job, self.child(fixture, changes), finalizer)
                finalizer.assert_called_once()
                status = json.loads((fixture.job / 'status.json').read_text())
                self.assertEqual(status['state'], 'failed')
                self.assertEqual(status['error'], 'FinalizeError: original publication failure')
                self.assertEqual(json.loads((fixture.job / 'failure.json').read_text())['error'], status['error'])
                self.assertTrue(fixture.performance()['incomplete'])

    def test_expected_serialization_failure_stays_inside_diagnostic_write(self):
        fixture = self.fixture()
        changes = {'shutdown': {'state': 'not_applicable'}, 'invalidMetadata': '\ud800'}
        finalizer = Mock(side_effect=lambda payload, result: result)
        jobs.execute(fixture.job, self.child(fixture, changes), finalizer)
        finalizer.assert_called_once()
        self.assertEqual(json.loads((fixture.job / 'status.json').read_text())['state'], 'completed')
        self.assertTrue(fixture.performance()['incomplete'])

    def test_deep_json_parse_failure_does_not_replace_publication(self):
        fixture = self.fixture()
        run = self.child(fixture, {'shutdown': {'state': 'not_applicable'}})
        def corrupted_after_verification(payload):
            result = run(payload)
            path = fixture.job / 'performance.json'
            wire = path.read_text()
            path.write_text(wire[:-1] + ',"deepDiagnostic":' + '['*2000 + '0' + ']'*2000 + '}')
            return result
        finalizer = Mock(side_effect=lambda payload, result: result)
        jobs.execute(fixture.job, corrupted_after_verification, finalizer)
        finalizer.assert_called_once()
        self.assertEqual(json.loads((fixture.job / 'status.json').read_text())['state'], 'completed')
        self.assertTrue(fixture.performance()['incomplete'])
        self.assertEqual(fixture.performance()['parent']['workerExitCode'], 0)

    def test_unknown_parent_fields_are_rejected_instead_of_merged(self):
        fixture = self.fixture()
        atomic_json(fixture.job / 'performance.json', {**fixture.prefix, 'parent': {
            'workerExitCode': 0, 'unexpectedAuthority': {'state': 'completed'}}})
        with patch.object(jobs, '_stop_worker'), patch.object(jobs, 'stop_process_group'):
            self.assertEqual(jobs.inspect(fixture.root, 'j1', 'cancel')['state'], 'cancelled')
        self.assertNotIn('unexpectedAuthority', fixture.performance()['parent'])
        self.assertTrue(fixture.performance()['incomplete'])


if __name__ == '__main__':
    unittest.main()
