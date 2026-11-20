"""CPU-only system fixtures; these are not generator products."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from runtime import jobs
from runtime.records import atomic_json


class PerformanceTests(unittest.TestCase):
    def test_terminal_budget_is_parent_owned_and_corrupt_child_values_are_diagnostic_only(self):
        for value in (-17,1800,True,'900'):
            with self.subTest(value=value):
                prefix={**self.prefix,'workerTimeoutSeconds':value}
                atomic_json(self.job/'performance.json',prefix)
                parent=jobs._parent_performance(self.job,self.payload,'worker_verification_only')
                parent['counters']['workerTimeoutSeconds']=900
                jobs._write_performance(self.job,parent)
                saved=self.performance()
                self.assertEqual(saved['workerTimeoutSeconds'],900)
                self.assertTrue(saved['diagnosticFailed'])
                self.assertEqual(saved['parent']['workerTimeoutSeconds'],900)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.job = self.root / 'jobs/j1'
        self.job.mkdir(parents=True)
        self.payload = {'root': str(self.root), 'jobId': 'j1', 'requestId': 'r1', 'operation': 'check', 'task': {'operation': 'check'}}
        atomic_json(self.job / 'request.json', {'payload': {**self.payload, 'task': {'operation': 'check'}},
                                              'runtimeSnapshot': '/system-test/runtime'})
        atomic_json(self.job / 'worker-request.json', self.payload)
        atomic_json(self.job / 'worker-process.json', {'pid': 123, 'processToken': 'finished-test-worker'})
        atomic_json(self.job / 'status.json', {'jobId': 'j1', 'state': 'running'})
        (self.job / 'events.jsonl').write_text('')
        self.prefix = {'schema': 'sceneagent-performance-v1', 'requestHash': jobs.payload_digest(self.payload),
            'operation': 'check', 'runtimeSnapshot': '/system-test/runtime',
            'worker': {'pid': 123, 'processToken': 'finished-test-worker'}, 'phase': 'shutdown_started',
            'groups': {'stateRead': {'seconds': .01, 'count': 2, 'failedCount': 0}},
            'shutdown': {'state': 'not_applicable'}, 'incomplete': True, 'inclusive': True}
        atomic_json(self.job / 'performance.json', self.prefix)

    def performance(self):
        return json.loads((self.job / 'performance.json').read_text())

    def test_parent_finalize_failure_retains_verified_child_and_real_error(self):
        file = self.root / 'report.json'
        file.write_text('{"passed":true}')
        script = self.root / 'publish-worker.py'
        script.write_text("""import json,pathlib,sys
sys.path.insert(0,sys.argv[1])
from runtime import jobs
work=pathlib.Path(sys.argv[2]);payload=json.loads((work/'worker-request.json').read_text())
jobs.start_performance(work,payload)
with jobs.measure('prepare'): pass
jobs.write_candidate(work,payload,{'passed':True},[pathlib.Path(payload['root'])/'report.json'],dict.fromkeys(('records','images','encoder','replicator'),True))
jobs.shutdown_started(False)
""")
        def runner(payload):
            return jobs.supervise(payload, lambda *_: [file], self.job,
                [sys.executable, str(script), str(Path(jobs.__file__).parent.parent), str(self.job)])
        finalizer = Mock(side_effect=RuntimeError('publication source failure'))
        jobs.execute(self.job, runner, finalizer)
        self.assertEqual(json.loads((self.job / 'status.json').read_text())['state'], 'failed')
        self.assertIn('publication source failure', json.loads((self.job / 'failure.json').read_text())['error'])
        self.assertEqual(self.performance()['parent']['publication']['state'], 'failed')
        self.assertEqual(self.performance()['parent']['publication']['count'], 1)
        self.assertEqual(self.performance()['parent']['workerExitCode'], 0)
        self.assertEqual(self.performance()['parent']['verification']['state'], 'completed')
        self.assertEqual(self.performance()['groups']['prepare']['count'], 1)
        self.assertEqual(self.performance()['terminalState'], 'failed')

    def test_cancel_wins_and_late_parent_cannot_replace_final_diagnostic(self):
        def runner(payload):
            with patch('runtime.jobs._stop_worker'), patch('runtime.jobs.stop_process_group'):
                jobs.inspect(self.root, 'j1', 'cancel')
            self.cancelled_bytes = (self.job / 'performance.json').read_bytes()
            return {'passed': True}
        finalizer = Mock(side_effect=AssertionError('must not publish cancelled work'))
        jobs.execute(self.job, runner, finalizer)
        finalizer.assert_not_called()
        self.assertEqual(self.performance()['terminalState'], 'cancelled')
        self.assertEqual(self.performance()['parent']['publication']['state'], 'not_executed')
        self.assertEqual((self.job / 'performance.json').read_bytes(), self.cancelled_bytes)

    def test_complete_wins_and_late_cancel_keeps_diagnostic_bytes(self):
        jobs.execute(self.job, lambda payload: {'passed': True}, lambda payload, result: result)
        before = (self.job / 'performance.json').read_bytes()
        self.assertEqual(self.performance()['terminalState'], 'completed')
        self.assertEqual(self.performance()['parent']['publication']['state'], 'completed')
        jobs.inspect(self.root, 'j1', 'cancel')
        self.assertEqual((self.job / 'performance.json').read_bytes(), before)

    def test_missing_corrupt_identity_and_diagnostic_write_failure_do_not_change_result(self):
        for mode in ('missing', 'corrupt', 'request', 'worker', 'write'):
            with self.subTest(mode=mode):
                atomic_json(self.job / 'status.json', {'jobId': 'j1', 'state': 'running'})
                prefix = json.loads(json.dumps(self.prefix))
                if mode == 'missing':
                    (self.job / 'performance.json').unlink(missing_ok=True)
                elif mode == 'corrupt':
                    (self.job / 'performance.json').write_text('{broken')
                else:
                    if mode == 'request': prefix['requestHash'] = 'wrong'
                    if mode == 'worker': prefix['worker']['processToken'] = 'wrong'
                    atomic_json(self.job / 'performance.json', prefix)
                original = jobs.atomic_json
                def write(path, value):
                    if mode == 'write' and path.name == 'performance.json':
                        raise OSError('diagnostic disk error')
                    original(path, value)
                with patch('runtime.jobs.atomic_json', side_effect=write):
                    jobs.execute(self.job, lambda payload: {'passed': True}, lambda payload, value: value)
                self.assertEqual(json.loads((self.job / 'status.json').read_text())['state'], 'completed')
                self.assertTrue(json.loads((self.job / 'result.json').read_text())['passed'])
                if mode != 'write':
                    self.assertTrue(self.performance()['incomplete'])
                    self.assertEqual(self.performance()['groups'], {})

        atomic_json(self.job / 'status.json', {'jobId': 'j1', 'state': 'running'})
        prefix = {**self.prefix, 'parent': {'workerExitCode': 0, 'verification': 'malformed diagnostic field'}}
        atomic_json(self.job / 'performance.json', prefix)
        with patch('runtime.jobs._stop_worker'), patch('runtime.jobs.stop_process_group'):
            result = jobs.inspect(self.root, 'j1', 'cancel')
        self.assertEqual(result['state'], 'cancelled')
        self.assertTrue(self.performance()['incomplete'])

    def test_job_prefix_stays_partial_until_publication_and_keeps_diagnostic_failure(self):
        parent = jobs._parent_performance(self.job, self.payload, 'job')
        parent['counters'].update(workerExitCode=0, verification={'state': 'completed'})
        jobs._write_performance(self.job, parent)
        self.assertTrue(self.performance()['incomplete'])
        with patch.object(jobs, 'atomic_json', side_effect=OSError('parent diagnostic write failed')):
            jobs._write_performance(self.job, parent)
        parent['counters']['publication'] = {'state': 'completed', 'seconds': .01, 'count': 1, 'failedCount': 0}
        jobs._write_performance(self.job, parent, {'state': 'completed'})
        self.assertEqual(self.performance()['terminalState'], 'completed')
        self.assertTrue(self.performance()['incomplete'])
        self.assertEqual(self.performance()['parent']['diagnosticFlush']['failedCount'], 1)

    def test_direct_supervise_is_worker_verification_only(self):
        file = self.root / 'report.json'
        file.write_text('{"passed":true}')
        script = self.root / 'fake-worker.py'
        script.write_text("""import json,os,pathlib,sys
sys.path.insert(0,sys.argv[1])
from runtime import jobs
work=pathlib.Path(sys.argv[2]); payload=json.loads((work/'worker-request.json').read_text())
jobs.start_performance(work,payload)
with jobs.measure('prepare'): pass
jobs.flush_performance('business_complete')
jobs.write_candidate(work,payload,{'passed':True},[pathlib.Path(payload['root'])/'report.json'],dict.fromkeys(('records','images','encoder','replicator'),True))
jobs.shutdown_started(False)
""")
        work = self.root / 'invocation'
        result = jobs.supervise(self.payload, lambda *_: [file], work,
                                [sys.executable, str(script), str(Path(jobs.__file__).parent.parent), str(work)])
        self.assertTrue(result['passed'])
        value = json.loads((work / 'performance.json').read_text())
        self.assertEqual(value['scope'], 'worker_verification_only')
        self.assertEqual(value['parent']['publication']['state'], 'not_applicable')
        self.assertEqual(value['parent']['workerExitCode'], 0)
        self.assertEqual(value['parent']['verification']['state'], 'completed')
        self.assertEqual(value['shutdown']['state'], 'not_applicable')

class TimingBehaviourTests(unittest.TestCase):
    def test_measure_is_inert_without_context_and_preserves_real_exception(self):
        with patch.object(jobs, '_timing', None), patch.object(jobs.time, 'perf_counter') as counter, \
             patch.object(jobs, 'atomic_json') as write:
            with jobs.measure('stateRead'):
                value = {'unchanged': [1, 2]}
            self.assertEqual(value, {'unchanged': [1, 2]})
            counter.assert_not_called()
            write.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary, jobs.performance_context(Path(temporary), {'operation': 'check'}):
            with jobs.measure('build'):
                with jobs.measure('stateRead'):
                    pass
            with self.assertRaisesRegex(RuntimeError, 'original business error'), jobs.measure('build'):
                raise RuntimeError('original business error')
            counters = jobs._timing['document']['groups']
            self.assertEqual(counters['build']['count'], 2)
            self.assertEqual(counters['build']['failedCount'], 1)
            self.assertGreaterEqual(counters['build']['seconds'], counters['stateRead']['seconds'])
            self.assertFalse((Path(temporary) / 'performance.json').exists())

    def test_snapshot_failure_is_diagnostic_and_next_prefix_retains_counts(self):
        with tempfile.TemporaryDirectory() as temporary, jobs.performance_context(Path(temporary), {'operation': 'check'}):
            with jobs.measure('prepare'):
                pass
            with patch.object(jobs, 'atomic_json', side_effect=OSError('test diagnostic disk error')):
                jobs.flush_performance('progress:test')
            jobs.shutdown_started(False)
            value = json.loads((Path(temporary) / 'performance.json').read_text())
            self.assertTrue(value['incomplete'])
            self.assertEqual(value['groups']['prepare']['count'], 1)
            self.assertEqual(value['groups']['diagnosticFlush']['count'], 1)
            self.assertEqual(value['groups']['diagnosticFlush']['failedCount'], 1)
            self.assertEqual(value['shutdown']['state'], 'not_applicable')

    def test_actual_record_commit_bytes_and_durability_calls_are_identical(self):
        from tests.test_native_records import begin,observation,COMMAND
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            def commit(measured):
                directory=root/('measured' if measured else 'plain');recorder,_=begin(directory)
                with patch.object(jobs,'_timing',None):
                    context=jobs.performance_context(root,{'operation':'rollout'}) if measured else __import__('contextlib').nullcontext()
                    with context,patch('runtime.records.os.fsync',wraps=os.fsync) as fsync,patch.object(jobs,'atomic_json') as diagnostic:
                        result=recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10,
                            truth_before=observation(0,truth=True),truth_after=observation(.02,truth=True))
                        count=fsync.call_count;diagnostic.assert_not_called()
                        if measured:
                            for name in ('recordWrite','recordIndex'):self.assertEqual(jobs._timing['document']['groups'][name]['count'],1)
                recorder.close('completed')
                return result,count,{name:(directory/name).read_bytes() for name in ('transitions.jsonl','records.json')}
            self.assertEqual(commit(False),commit(True))


    def test_shutdown_start_is_real_and_nonreturning_exit_has_no_fabricated_completion(self):
        from runtime import simulation
        def fast_shutdown(**kwargs):
            raise SystemExit(kwargs['exit_code'])
        app = Mock(close=Mock(side_effect=fast_shutdown))
        with tempfile.TemporaryDirectory() as temporary, jobs.performance_context(Path(temporary), {'operation': 'check'}), \
             patch.object(simulation, '_app', app):
            with self.assertRaises(SystemExit) as ended:
                simulation.shutdown(7)
            self.assertEqual(ended.exception.code, 7)
            app.close.assert_called_once_with(wait_for_replicator=True, skip_cleanup=False, exit_code=7)
            value = json.loads((Path(temporary) / 'performance.json').read_text())
            self.assertEqual(value['shutdown']['state'], 'started')
            self.assertNotIn('seconds', value['shutdown'])
            self.assertNotIn('terminalState', value)
            atomic_json(Path(temporary) / 'worker-process.json', value['worker'])
            parent = jobs._parent_performance(Path(temporary), {'operation': 'check'}, 'worker_verification_only')
            jobs._write_performance(Path(temporary), parent, identity=value['worker'], exit_code=7,
                                    observed_exit=value['shutdown']['startedCounter'] + .25)
            observed = json.loads((Path(temporary) / 'performance.json').read_text())
            self.assertEqual(observed['shutdown']['measurement'], 'observed_upper_bound')
            self.assertEqual(observed['shutdown']['seconds'], .25)
            self.assertTrue(observed['incomplete'])

    def test_real_export_and_observe_keep_identical_data_and_read_counts(self):
        from contextlib import nullcontext
        from tests.test_native_environment import NativeEnvironmentTests
        def execute(measured):
            fixture=NativeEnvironmentTests();self.addCleanup(fixture.doCleanups)
            env,art=fixture.environment();env.io=env._discover_io()
            env._closed=False;env._stream_cache={};env.latest_camera_frames={};env.recorder=None
            env._clock=Mock(return_value={'time':0.,'simulationTime':.01,'timeOrigin':.01,'physicsStep':0,'stageId':9})
            modules=fixture.backend()
            with patch.object(jobs,'_timing',None):
                context=jobs.performance_context(Path('/unused-system-test'),{'operation':'check'}) if measured else nullcontext()
                with context,patch.dict(sys.modules,modules),patch.object(jobs,'atomic_json') as diagnostic:
                    value=[env.export_state(),env.observe()]
                    diagnostic.assert_not_called()
                    if measured:self.assertGreaterEqual(jobs._timing['document']['groups']['stateRead']['count'],2)
            return json.dumps(value,sort_keys=True,default=lambda item:item.tolist()),env._clock.call_count,art.calls
        self.assertEqual(execute(False),execute(True))


    def test_control_step_keeps_frozen_physics_calls_and_never_flushes_per_step(self):
        from contextlib import nullcontext
        from tests.test_native_environment import NativeEnvironmentTests
        def execute(measured):
            fixture=NativeEnvironmentTests();self.addCleanup(fixture.doCleanups)
            env,art,modules=fixture.step_fixture()
            with patch.object(jobs,'_timing',None):
                context=jobs.performance_context(env.directory,{'operation':'rollout'}) if measured else nullcontext()
                with context,patch.dict(sys.modules,modules),patch.object(jobs,'atomic_json') as diagnostic:
                    value=env.step({'motor':[.2]},.02)
                    diagnostic.assert_not_called()
                    if measured:self.assertEqual(jobs._timing['document']['groups']['physicsStep']['count'],4)
            return value,[modules['isaacsim.core.simulation_manager'].SimulationManager.step.call_count,env.capture_frame.call_count],art.calls
        plain,measured=execute(False),execute(True)
        self.assertEqual(plain,measured);self.assertEqual(measured[1],[4,1])


    def test_actual_render_keeps_frame_and_existing_render_state_and_write_counts(self):
        from contextlib import nullcontext
        from tests.test_capture_v5 import CaptureBoundaryTests
        def execute(measured):
            fixture=CaptureBoundaryTests();self.addCleanup(fixture.doCleanups);env=fixture.fixture()
            env.camera_resources['front']['writer']=Mock();before=env.export_state()
            with patch.object(jobs,'_timing',None):
                context=jobs.performance_context(env.directory,{'operation':'observe'}) if measured else nullcontext()
                with context,patch.object(jobs,'atomic_json') as diagnostic:
                    frame=env.capture_frame();diagnostic.assert_not_called()
                    if measured:
                        for name,count in (('render',1),('imageWrite',2),('videoEncode',1)):
                            self.assertEqual(jobs._timing['document']['groups'][name]['count'],count)
            self.assertEqual(env.export_state(),before)
            return frame,env.render.call_count,env.camera_resources['front']['writer'].append_data.call_count,(env.directory/frame['path']).read_bytes()
        self.assertEqual(execute(False),execute(True))


    def test_terminated_real_worker_keeps_last_prefix_and_actual_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'worker.py'
            script.write_text("""import json,pathlib,sys,time
sys.path.insert(0,sys.argv[1])
from runtime import jobs
work=pathlib.Path(sys.argv[2]);payload=json.loads((work/'worker-request.json').read_text())
jobs.start_performance(work,payload)
with jobs.measure('prepare'): pass
jobs.flush_performance('progress:test-prefix')
time.sleep(30)
""")
            work = root / 'invocation'
            with self.assertRaises(TimeoutError):
                jobs.supervise({'root': str(root), 'operation': 'prepare'}, lambda *_: [], work,
                    [sys.executable, str(script), str(Path(jobs.__file__).parent.parent), str(work)], timeout=.3)
            value = json.loads((work / 'performance.json').read_text())
            self.assertEqual(value['groups']['prepare']['count'], 1)
            self.assertNotIn('workerExitCode', value['parent'])
            self.assertNotIn('observedExitCounter', value['parent'])
            self.assertNotEqual(value['parent']['cleanupReturnCode'], 0)
            self.assertIn('cleanupCompletedCounter', value['parent'])
            self.assertTrue(value['incomplete'])
            self.assertEqual(value['unflushedTail'], 'unknown')
            self.assertEqual(value['parent']['publication']['state'], 'not_applicable')
            self.assertNotIn('verification', value['parent'])

    def test_sdk_timing_wraps_only_first_actual_initialization(self):
        import ast
        from types import ModuleType, SimpleNamespace
        from runtime import simulation
        function = next(node for node in ast.parse(Path(simulation.__file__).read_text()).body
                        if isinstance(node, ast.FunctionDef) and node.name == '_ensure_app')
        app = SimpleNamespace(config={'fast_shutdown': True})
        isaac = ModuleType('isaacsim')
        isaac.SimulationApp = Mock(return_value=app)
        extension = ModuleType('isaacsim.core.experimental.utils.app')
        extension.enable_extension = Mock()
        modules = {}
        for name in ('isaacsim.core', 'isaacsim.core.experimental', 'isaacsim.core.experimental.utils'):
            modules[name] = ModuleType(name)
        modules.update({'isaacsim': isaac, 'isaacsim.core.experimental.utils.app': extension})
        namespace = {'_app': None, '_mdl_configuration': None, 'measure': jobs.measure, 'VERSION': simulation.VERSION,
                     'WIDTH': simulation.WIDTH, 'HEIGHT': simulation.HEIGHT, 'os': os,
                     'sys': SimpleNamespace(version_info=(3, 12)),
                     'importlib': SimpleNamespace(metadata=SimpleNamespace(version=lambda name: simulation.VERSION))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), simulation.__file__, 'exec'), namespace)
        with tempfile.TemporaryDirectory() as temporary, jobs.performance_context(Path(temporary), {'operation': 'check'}), \
             patch.dict(sys.modules, modules), patch.dict(os.environ, {'SCENEAGENT_GPU': '3',
                 'CUDA_VISIBLE_DEVICES': 'GPU-30000000-0000-0000-0000-000000000000'}):
            self.assertIs(namespace['_ensure_app'](), app)
            self.assertIs(namespace['_ensure_app'](), app)
            isaac.SimulationApp.assert_called_once()
            self.assertEqual(isaac.SimulationApp.call_args.args[0]['active_gpu'], 3)
            self.assertEqual(isaac.SimulationApp.call_args.args[0]['physics_gpu'], 0)
            self.assertEqual(extension.enable_extension.call_count, 1)  # RTX only; retired Lula is not enabled.
            self.assertEqual(jobs._timing['document']['groups']['sdkInitialization']['count'], 1)
            self.assertFalse((Path(temporary) / 'performance.json').exists())


    @unittest.skipUnless(sys.platform == 'linux', 'real cancellation/death identities require Linux')
    def test_cancel_and_supervisor_death_preserve_prefix_after_actual_process_stop(self):
        import signal
        import time
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / 'code'
            snapshot.mkdir()
            script = snapshot / 'worker.py'
            script.write_text("""import json,pathlib,sys,time
sys.path.insert(0,""" + repr(str(Path(jobs.__file__).parent.parent)) + """)
from runtime import jobs
if sys.argv[1]=='--test-child':
    work=pathlib.Path(sys.argv[2]);payload=json.loads((work/'worker-request.json').read_text())
    jobs.start_performance(work,payload)
    with jobs.measure('prepare'): pass
    jobs.flush_performance('progress:latest-child-prefix')
    time.sleep(30)
else:
    work=pathlib.Path(sys.argv[-1])
    jobs.execute(work,lambda payload: jobs.supervise(payload,lambda *_: [],work,
        [sys.executable,__file__,'--test-child',str(work)]))
""")
            for mode in ('cancel', 'death'):
                with self.subTest(mode=mode):
                    status = jobs.submit(snapshot, root, {'root': str(root), 'jobId': mode, 'requestId': mode,
                        'task': {'operation': 'prepare'}})
                    work = root / 'jobs' / mode
                    deadline = time.monotonic() + 5
                    self.addCleanup(jobs.stop_process_group, status)
                    while (not (work / 'performance.json').exists() or not (work / 'worker-process.json').exists()) and time.monotonic() < deadline:
                        time.sleep(.02)
                    prefix = json.loads((work / 'performance.json').read_text())
                    self.addCleanup(jobs.stop_process_group, prefix['worker'])
                    if mode == 'death':
                        os.killpg(status['pid'], signal.SIGKILL)
                        while jobs._token(status['pid']) == status['processToken'] and time.monotonic() < deadline:
                            time.sleep(.02)
                    result = jobs.inspect(root, mode, 'cancel' if mode == 'cancel' else 'status')
                    value = json.loads((work / 'performance.json').read_text())
                    self.assertEqual(result['state'], 'cancelled' if mode == 'cancel' else 'failed')
                    self.assertEqual(value['terminalState'], result['state'])
                    self.assertEqual(value['groups']['prepare'], prefix['groups']['prepare'])
                    self.assertEqual(value['parent']['publication'], {'state': 'not_executed'})
                    self.assertTrue(value['incomplete'])
                    self.assertIsNone(jobs._token(prefix['worker']['pid']))
                    self.assertIsNone(jobs._token(status['pid']))


class CleanupPerformanceTests(unittest.TestCase):
    def fixture(self):
        fixture=PerformanceTests();fixture.setUp();self.addCleanup(fixture.doCleanups);return fixture

    def test_cleanup_zero_is_not_natural_exit_and_survives_recovery(self):
        fixture=self.fixture();parent=jobs._parent_performance(fixture.job,fixture.payload,'worker_verification_only')
        jobs._write_performance(fixture.job,parent,cleanup_return_code=0,cleanup_completed=12.5)
        value=fixture.performance();self.assertEqual(value['parent']['cleanupReturnCode'],0)
        self.assertEqual(value['parent']['cleanupCompletedCounter'],12.5);self.assertTrue(value['incomplete'])
        self.assertNotIn('workerExitCode',value['parent']);self.assertNotIn('observedExitCounter',value['parent'])
        self.assertNotEqual(value['shutdown']['state'],'observed_exit')
        recovered=jobs._parent_performance(fixture.job,fixture.payload,'job')
        jobs._write_performance(fixture.job,recovered,status={'state':'failed'})
        self.assertEqual(fixture.performance()['parent']['cleanupReturnCode'],0)
        self.assertNotIn('workerExitCode',fixture.performance()['parent'])

    def test_optional_cleanup_fields_are_strict_and_old_records_stay_compatible(self):
        self.assertEqual(jobs._performance_parent_fields({}),{})
        old={'workerExitCode':0,'observedExitCounter':1.25}
        self.assertEqual(jobs._performance_parent_fields(old),old)
        for key,valid,bad in [('cleanupReturnCode',-15,[None,True,0.,'0',[],10**400]),
                              ('cleanupCompletedCounter',1.25,[None,True,'1',[],float('inf'),float('nan'),-1])]:
            self.assertEqual(jobs._performance_parent_fields({key:valid})[key],valid)
            for value in bad:
                with self.subTest(key=key,value=repr(value)),self.assertRaises(ValueError):jobs._performance_parent_fields({key:value})


if __name__ == '__main__':
    unittest.main()
