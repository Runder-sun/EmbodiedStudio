from pathlib import Path
import tempfile
import unittest
from runtime.jobs import submit, inspect, execute


class JobsTests(unittest.TestCase):
    def test_owner_identity_is_flushed_only_for_new_or_changed_process_tokens(self):
        import json
        from unittest.mock import patch
        from runtime import jobs
        class Child:
            pid=12345
            returncode=None
            polls=0
            def poll(self):
                self.polls+=1
                if self.polls>=8:self.returncode=0
                return self.returncode
            def wait(self,timeout=None):return self.returncode
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);child=Child();writes=[];atomic=jobs.atomic_json
            def members(_):
                if child.returncode is not None:return {}
                value={child.pid:'own-token'}
                if child.polls in (3,4):value[23456]='first-token'
                if child.polls in (6,7):value[23456]='reused-token'
                return value
            def save(path,value):
                atomic(path,value)
                if path.name=='worker-process.json':writes.append(json.loads(path.read_text()))
            with patch('fcntl.flock'),patch.object(jobs.subprocess,'Popen',return_value=child), \
                 patch.object(jobs,'_token',return_value='own-token'),patch.object(jobs,'_group_members',side_effect=members), \
                 patch.object(jobs,'stop_process_group'),patch.object(jobs,'verify_candidate',return_value={'passed':True}), \
                 patch.object(jobs,'_write_performance'),patch.object(jobs.time,'sleep'),patch.object(jobs,'atomic_json',side_effect=save):
                result=jobs.supervise({'root':str(root),'operation':'check'},lambda *_:[],root/'job',command=['fixture'])
            self.assertEqual(result,{'passed':True})
            self.assertEqual(len(writes),3)
            self.assertEqual([value['members'].get('23456') for value in writes],[None,'first-token','reused-token'])
            self.assertTrue(all(value['pid']==child.pid and value['processToken']=='own-token' for value in writes))

    def test_consumer_without_parent_sdk_still_requires_verified_artifacts(self):
        from runtime import jobs
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); work = root/'job'; work.mkdir()
            receipt = root/'native-interface-result.json'; receipt.write_text('{"fixture":"verified child"}')
            payload = {'root':str(root), 'operation':'consume'}
            result = {'passed':True}
            jobs.write_candidate(work, payload, result, [receipt],
                dict.fromkeys(('records','images','encoder','replicator'), True),
                {'terminationMode':'no_sdk_application'})
            self.assertEqual(jobs.verify_candidate(work, payload, 0, lambda *_:[receipt]), result)
            def missing_child(*_):
                raise ValueError('actual child SDK evidence is incomplete')
            with self.assertRaisesRegex(ValueError, 'child SDK evidence'):
                jobs.verify_candidate(work, payload, 0, missing_child)
            with self.assertRaisesRegex(ValueError, 'file set'):
                jobs.verify_candidate(work, payload, 0, lambda *_:[])
            receipt.write_text('{"fixture":"changed"}')
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                jobs.verify_candidate(work, payload, 0, lambda *_:[receipt])

    def test_no_sdk_consumer_contract_does_not_allow_rollout_without_sdk(self):
        from runtime import jobs
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); work = root/'job'; work.mkdir()
            receipt = root/'report.json'; receipt.write_text('{}')
            payload = {'root':str(root), 'operation':'rollout'}
            jobs.write_candidate(work, payload, {'passed':True}, [receipt],
                dict.fromkeys(('records','images','encoder','replicator'), True),
                {'terminationMode':'no_sdk_application'})
            with self.assertRaisesRegex(ValueError, 'termination contract'):
                jobs.verify_candidate(work, payload, 0, lambda *_:[receipt])

    def test_only_native_host_shaders_use_gpu_driver_scoped_cache_and_reject_symlink_escape(self):
        from unittest.mock import patch
        from runtime import jobs
        device={'index':3,'uuid':'GPU-30000000-0000-0000-0000-000000000000'}
        with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryDirectory() as outside:
            root=Path(directory).resolve();work=root/'job'
            with patch.object(jobs,'assign_device',return_value='570.211.01'), \
                 patch.object(jobs.subprocess,'Popen',side_effect=OSError('stop before SDK')) as launch:
                with self.assertRaisesRegex(OSError,'stop before SDK'):
                    jobs.supervise({'root':str(root),'operation':'observe','device':device},lambda *_:[],work)
                command=launch.call_args.args[0];environment=launch.call_args.kwargs['env']
            shader=root/'cache/isaac-6-trajectory-v5/rtx'/device['uuid']/'570.211.01'
            for setting,folder in [('shaderCachePath','shadercache'),('driverShaderCachePath','nv_shadercache')]:
                self.assertIn('--/rtx/shaderDb/'+setting+'='+str(shader/folder),command)
                self.assertTrue((shader/folder).is_dir())
            self.assertEqual(environment['XDG_CACHE_HOME'],str(work/'xdg-cache'))
            import shutil
            shutil.rmtree(root/'cache');(root/'cache').symlink_to(outside,target_is_directory=True)
            with patch.object(jobs,'assign_device',return_value='570.211.01'),patch.object(jobs.subprocess,'Popen') as launch:
                with self.assertRaisesRegex(ValueError,'shader cache'):
                    jobs.supervise({'root':str(root),'operation':'observe','device':device},lambda *_:[],root/'blocked')
                launch.assert_not_called()
            self.assertEqual(list(Path(outside).iterdir()),[])

    def test_worker_timeout_requires_a_finite_positive_non_boolean_duration_before_launch(self):
        from unittest.mock import patch
        from runtime import jobs
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for timeout in (True,False,0,-1,float('nan'),float('inf'),'900'):
                with self.subTest(timeout=timeout),patch.dict(jobs.os.environ,{},clear=True), \
                     patch.object(jobs.subprocess,'Popen') as launch,self.assertRaisesRegex(ValueError,'timeout'):
                    jobs.supervise({'root':str(root),'operation':'check'},lambda *_:[],
                        root/'job',command=['unused'],timeout=timeout)
                self.assertEqual(launch.call_count,0)

    def test_public_worker_budget_preserves_absent_request_fields_and_rejects_invalid_values(self):
        from runtime import jobs
        payload={'operation':'rollout'}
        self.assertEqual(jobs.worker_timeout(payload),900)
        self.assertEqual(payload,{'operation':'rollout'})
        self.assertEqual(jobs.worker_timeout({**payload,'workerTimeoutSeconds':3600.5}),3600.5)
        for value in (None,True,False,0,-1,float('nan'),float('inf'),'900',10**400):
            with self.subTest(value=repr(value)[:40]),self.assertRaisesRegex(ValueError,'timeout'):
                jobs.worker_timeout({**payload,'workerTimeoutSeconds':value})

    def test_public_worker_budget_is_the_actual_supervisor_deadline_and_is_recorded(self):
        from unittest.mock import patch,Mock
        from runtime import jobs
        class Child:
            pid=12345
            returncode=None
            polls=0
            def poll(self):
                self.polls+=1
                if self.polls>1:self.returncode=0
                return self.returncode
            def wait(self,timeout=None):return self.returncode
        for budget,times_out in ((1800,False),(10,True)):
            with self.subTest(budget=budget),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);child=Child();payload={'root':str(root),'operation':'check','workerTimeoutSeconds':budget}
                with patch.dict(jobs.os.environ,{},clear=True),patch('fcntl.flock'), \
                     patch.object(jobs.subprocess,'Popen',return_value=child), \
                     patch.object(jobs,'_token',return_value='own-token'), \
                     patch.object(jobs,'_group_members',return_value={}), \
                     patch.object(jobs,'stop_process_group',side_effect=lambda *a:setattr(child,'returncode',0)), \
                     patch.object(jobs,'verify_candidate',return_value={'passed':True}) as verify, \
                     patch.object(jobs,'_write_performance') as recorded, \
                     patch.object(jobs.time,'monotonic',side_effect=[0.,1000.]),patch.object(jobs.time,'sleep'):
                    if times_out:
                        with self.assertRaisesRegex(TimeoutError,'wall-clock'):
                            jobs.supervise(payload,lambda *_:[],root/'job',command=['fixture'])
                        verify.assert_not_called()
                    else:self.assertEqual(jobs.supervise(payload,lambda *_:[],root/'job',command=['fixture']),{'passed':True})
                    self.assertEqual(recorded.call_args.args[1]['counters']['workerTimeoutSeconds'],budget)
                self.assertEqual(child.returncode,0)

    def test_candidate_budget_matches_public_request_and_old_absence_is_compatible(self):
        import json
        from runtime import jobs
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=root/'job';work.mkdir();file=root/'report.json';file.write_text('{}')
            for value in (None,2400):
                payload={'root':str(root),'operation':'observe'}
                if value is not None:payload['workerTimeoutSeconds']=value
                with jobs.performance_context(work,payload):
                    self.assertEqual(jobs._timing['document']['workerTimeoutSeconds'],value or 900)
                jobs.write_candidate(work,payload,{'passed':True},[file],dict.fromkeys(('records','images','encoder','replicator'),True))
                path=work/'candidate.json';saved=json.loads(path.read_text())
                self.assertEqual(saved['workerTimeoutSeconds'],value or 900)
                self.assertEqual(jobs.verify_candidate(work,payload,0,lambda *_:[file]),{'passed':True})
                changed={**saved,'workerTimeoutSeconds':1};path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError,'timeout|budget'):jobs.verify_candidate(work,payload,0,lambda *_:[file])
                saved.pop('workerTimeoutSeconds');path.write_text(json.dumps(saved))
                if value is None:self.assertEqual(jobs.verify_candidate(work,payload,0,lambda *_:[file]),{'passed':True})
                else:
                    with self.assertRaisesRegex(ValueError,'timeout|budget'):jobs.verify_candidate(work,payload,0,lambda *_:[file])

    def test_worker_failure_keeps_real_exit_and_exposes_a_bounded_source_error(self):
        import json
        from runtime.jobs import verify_candidate
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            error = "AttributeError: 'World' object has no attribute 'support'"
            failure = directory / 'worker-failure.json'
            failure.write_text(json.dumps({'error': error, 'traceback': 'full original traceback'}))
            with self.assertRaisesRegex(RuntimeError, '7.*World.*support'):
                verify_candidate(directory, {}, 7, None)
            self.assertEqual(json.loads(failure.read_text())['traceback'], 'full original traceback')
            failure.write_text(json.dumps({'error': 'long:' + 'x' * 10000, 'traceback': 'kept'}))
            with self.assertRaises(RuntimeError) as raised:
                verify_candidate(directory, {}, 7, None)
            self.assertLess(len(str(raised.exception)), 2100)
            failure.write_text('{incomplete')
            with self.assertRaisesRegex(RuntimeError, 'Isaac worker exit is not success: -11$'):
                verify_candidate(directory, {}, -11, None)
            failure.unlink()
            with self.assertRaisesRegex(RuntimeError, 'Isaac worker exit is not success: -9$'):
                verify_candidate(directory, {}, -9, None)

    def test_rtx_allocation_log_is_attached_to_a_failed_exit_without_reclassifying_the_crash(self):
        import json
        from runtime.jobs import verify_candidate
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            log = directory / 'kit.log'
            evidence = '2026-10-22T13:19:45Z [29,792ms] [Error] [omni.rtx] VkResult: ERROR_OUT_OF_DEVICE_MEMORY'
            log.write_text('x' * 300000 + '\n' + evidence + '\nFatal Python error: Segmentation fault\n')
            failure = directory / 'worker-failure.json'
            failure.write_text(json.dumps({'error': 'original worker error', 'traceback': 'original stack'}))
            with self.assertRaises(RuntimeError) as raised:
                verify_candidate(directory, {}, -11, None)
            message = str(raised.exception)
            self.assertIn('Isaac worker exit is not success: -11: original worker error', message)
            self.assertIn('日志记录 RTX 显存分配失败', message)
            self.assertIn(str(log), message)
            self.assertIn(evidence, message)
            self.assertLess(len(message), 4000)
            self.assertEqual(json.loads(failure.read_text())['traceback'], 'original stack')
            self.assertTrue(log.read_text().endswith('Fatal Python error: Segmentation fault\n'))
            failure.unlink()
            log.write_text('Fatal Python error: Segmentation fault\nERROR_OUT_OF_DEVICE_MEMORY in user text\n')
            with self.assertRaisesRegex(RuntimeError, 'Isaac worker exit is not success: -11$'):
                verify_candidate(directory, {}, -11, None)
            log.unlink()
            log.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'Isaac worker exit is not success: -9$'):
                verify_candidate(directory, {}, -9, None)

    def test_worker_result_is_rejected_until_exit_and_fixed_files_validate(self):
        import json
        import sys
        from runtime.jobs import supervise
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / 'report.json'
            file.write_text('{"passed":true}')
            payload = {'root': str(root), 'operation': 'check', 'requestId': 'r1'}
            script = root / 'fake.py'
            script.write_text("""import json,hashlib,pathlib,sys,time,os
root=pathlib.Path(sys.argv[1]);work=pathlib.Path(sys.argv[2]);mode=sys.argv[3]
(work/'cache-paths.json').write_text(json.dumps({key:os.environ.get(key) for key in ('WARP_CACHE_PATH','TORCH_EXTENSIONS_DIR')}))
payload=json.loads((work/'worker-request.json').read_text())
data=(root/'report.json').read_bytes()
value={'schema':'sceneagent-worker-candidate-v3','engine':'isaacsim','physics':'physx','operation':'check','requestHash':hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest(),'businessExitCode':0,'terminationMode':'official_fast_shutdown','fullVendorCleanupVerified':False,'barriers':dict.fromkeys(['records','images','encoder','replicator'],True),'result':{'passed':True},'files':{'report.json':{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}}
value['workerTimeoutSeconds']=payload.get('workerTimeoutSeconds',900)
if mode=='missing': value['files']={}
if mode=='corrupt': value['files']['report.json']['sha256']='0'*64
if mode=='identity': value['requestHash']='wrong'
if mode=='barrier': value['barriers']['encoder']=False
if mode=='fake_true': value['barriers']['encoder']=1
(work/'candidate.json').write_text(json.dumps(value))
if mode=='hang': time.sleep(10)
sys.exit(7 if mode=='seven' else 0)
""")
            def invoke(mode):
                work = root / mode
                return supervise(payload, lambda payload, result: [file], work,
                    [sys.executable, str(script), str(root), str(work), mode], timeout=.25 if mode == 'hang' else 5)
            self.assertTrue(invoke('ok')['passed'])
            expected_cache = {key:str(root.resolve()/'cache/isaac-6-trajectory-v5'/name)
                              for key,name in (('WARP_CACHE_PATH','warp'),('TORCH_EXTENSIONS_DIR','torch-extensions'))}
            self.assertEqual(json.loads((root/'ok/cache-paths.json').read_text()), expected_cache)
            for mode in ('missing', 'corrupt', 'identity', 'barrier', 'fake_true', 'seven', 'hang'):
                with self.subTest(mode=mode), self.assertRaises((ValueError, RuntimeError, TimeoutError)):
                    invoke(mode)
                self.assertTrue((root / mode / 'candidate.json').exists())
            self.assertEqual(json.loads((root/'missing/cache-paths.json').read_text()), expected_cache)

    @unittest.skipUnless(__import__('sys').platform == 'linux', 'process-group membership is checked on the remote Linux host')
    def test_orphan_child_is_rejected_and_terminated(self):
        import sys
        from runtime.jobs import supervise, _group_members
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'orphan.py'
            script.write_text("import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'])")
            payload = {'root': str(root), 'operation': 'check'}
            with self.assertRaisesRegex(RuntimeError, 'running child'):
                supervise(payload, lambda payload, result: [], root / 'work', [sys.executable, str(script)], timeout=5)
            import json
            identity = json.loads((root / 'work/worker-process.json').read_text())
            self.assertEqual(_group_members(identity['pid']), {})

    @unittest.skipUnless(__import__('sys').platform == 'linux', 'native job cancellation is checked on Linux')
    def test_cancel_and_supervisor_death_confirm_the_kit_group_ended(self):
        import json
        import time
        from runtime.jobs import _token
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'code'
            snapshot.mkdir()
            (snapshot / 'worker.py').write_text("""import json,pathlib,subprocess,sys,time
job=pathlib.Path(sys.argv[-1]);child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
fields=pathlib.Path(f'/proc/{child.pid}/stat').read_text().split(') ',1)[1].split()
(job/'worker-process.json').write_text(json.dumps({'pid':child.pid,'processToken':fields[19],'members':{str(child.pid):fields[19]}}))
if job.name=='die': time.sleep(.1)
else: time.sleep(30)
""")
            for name in ('cancel', 'die'):
                submitted = submit(snapshot, root, {'jobId': name, 'requestId': name,
                    'task': {'operation': 'health'}})
                job = root / 'jobs' / name
                deadline = time.monotonic() + 5
                while not (job / 'worker-process.json').exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                identity = json.loads((job / 'worker-process.json').read_text())
                if name == 'die':
                    while _token(submitted['pid']) == submitted['processToken'] and time.monotonic() < deadline:
                        time.sleep(.02)
                state = inspect(root, name, 'cancel' if name == 'cancel' else 'status')
                self.assertEqual(state['state'], 'cancelled' if name == 'cancel' else 'failed')
                self.assertIsNone(_token(identity['pid']))
                self.assertIsNone(state['result'])

    def test_reused_process_identity_is_never_signalled(self):
        from unittest.mock import patch
        from runtime.jobs import stop_process_group
        with patch('runtime.jobs._token', return_value='new'), patch('runtime.jobs.os.killpg') as kill:
            stop_process_group({'pid': 123, 'processToken': 'old', 'members': {'123': 'old'}})
            kill.assert_not_called()

    def test_late_cancel_does_not_replace_completed_result(self):
        import json
        from runtime.records import atomic_json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root / 'jobs/j1'
            job.mkdir(parents=True)
            (job / 'events.jsonl').write_text('')
            atomic_json(job / 'status.json', {'jobId': 'j1', 'state': 'completed'})
            atomic_json(job / 'result.json', {'passed': True})
            value = inspect(root, 'j1', 'cancel')
            self.assertEqual(value['state'], 'completed')
            self.assertTrue(value['result']['passed'])
            self.assertEqual(value['events'], [])

    def test_terminal_job_releases_only_its_confirmed_worker_reservation(self):
        import json
        from unittest.mock import patch
        from runtime import jobs

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root / 'jobs/j1'
            job.mkdir(parents=True)
            device = {'index': 0, 'uuid': 'GPU-00000000-0000-0000-0000-000000000000'}
            ledger = root / 'runtime' / ('isaac-' + device['uuid'] + '-memory')
            ledger.mkdir(parents=True)
            lease_id = 'a' * 32
            reservation = ledger / (lease_id + '.json')
            other = ledger / ('b' * 32 + '.json')
            for action, live_lease in (('cancel', False), ('status', False), ('cancel', True)):
                reservation.with_suffix('.released').unlink(missing_ok=True)
                (job / 'events.jsonl').write_text('')
                (job / 'status.json').write_text(json.dumps({'jobId': 'j1', 'state': 'running',
                    'pid': 900, 'processToken': 'supervisor-token'}))
                (job / 'worker-request.json').write_text(json.dumps({'device': device, 'operation': 'rollout'}))
                (job / 'worker-process.json').write_text(json.dumps({'pid': 901, 'processToken': 'worker-token',
                    'members': {'901': 'worker-token'}, 'gpuReservationId': lease_id}))
                for record in (reservation, other):
                    record.write_text(json.dumps({'pid': 900, 'device': device, 'peakMiB': 10000}))
                with patch.object(jobs, 'stop_process_group'), patch.object(jobs, '_token', return_value=None), \
                     patch('runtime.gpu_reservation.process_lease', return_value=lease_id if live_lease else None), \
                     patch('runtime.gpu_reservation.Path.glob', return_value=[Path('/proc/901')]):
                    result = jobs.inspect(root, 'j1', action)
                self.assertEqual(result['state'], 'cancelled' if action == 'cancel' else 'failed')
                self.assertEqual(reservation.exists(), live_lease)
                self.assertTrue(other.exists())

    def test_cancel_winning_during_runner_does_not_publish_success(self):
        import json
        from runtime.records import atomic_json
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            (job / 'events.jsonl').write_text('')
            atomic_json(job / 'request.json', {'payload': {'jobId': 'j1', 'requestId': 'r1', 'task': {'operation': 'health'}}})
            atomic_json(job / 'status.json', {'jobId': 'j1', 'state': 'running'})
            def cancelled(payload):
                atomic_json(job / 'status.json', {'jobId': 'j1', 'state': 'cancelled'})
                return {'ready': True}
            from unittest.mock import Mock
            finalizer = Mock(side_effect=AssertionError('cancelled result must never finalize artifacts'))
            execute(job, cancelled, finalizer)
            finalizer.assert_not_called()
            self.assertEqual(json.loads((job / 'status.json').read_text())['state'], 'cancelled')
            self.assertFalse((job / 'result.json').exists())
            self.assertNotIn('result', [json.loads(line)['type'] for line in (job / 'events.jsonl').read_text().splitlines()])

    def test_dead_process_snapshot_cannot_overwrite_new_completed_receipt(self):
        import json
        from unittest.mock import patch
        from runtime.records import atomic_json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root / 'jobs/j1'
            job.mkdir(parents=True)
            (job / 'events.jsonl').write_text('')
            atomic_json(job / 'status.json', {'jobId': 'j1', 'state': 'running', 'pid': 123, 'processToken': 'old'})
            def completed_before_dead_check(pid):
                atomic_json(job / 'status.json', {'jobId': 'j1', 'state': 'completed'})
                return None
            with patch('runtime.jobs._token', completed_before_dead_check):
                self.assertEqual(inspect(root, 'j1')['state'], 'completed')
            self.assertEqual(json.loads((job / 'status.json').read_text())['state'], 'completed')

    def test_idempotent_request_retains_input_and_rejects_different_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'code'
            snapshot.mkdir()
            (snapshot / 'worker.py').write_text('import time; time.sleep(.01)\n')
            request = {'jobId': 'j1', 'requestId': 'r1', 'generationId': 'g1',
                       'task': {'operation': 'health'}}
            first = submit(snapshot, root, request)
            second = submit(snapshot, root, request)
            self.assertEqual(first['jobId'], second['jobId'])
            self.assertEqual(submit(snapshot, root, dict(request, jobId='different-job'))['jobId'], 'j1')
            self.assertTrue((root / 'jobs/j1/request.json').exists())
            with self.assertRaisesRegex(ValueError, 'different'):
                submit(snapshot, root, dict(request, task={'operation': 'build'}))
            result = inspect(root, 'j1')
            self.assertIn(result['state'], ('running', 'failed'))

    def legacy_request(self, root, request):
        import hashlib,json
        from runtime.records import atomic_json
        digest=hashlib.sha256(json.dumps({key:value for key,value in request.items() if key!='jobId'},sort_keys=True,allow_nan=False).encode()).hexdigest()
        job=root/'jobs'/request['jobId'];job.mkdir(parents=True)
        atomic_json(job/'request.json',{'payload':request,'requestHash':digest,'runtimeSnapshot':'frozen-legacy-fixture'})
        atomic_json(job/'status.json',{'jobId':request['jobId'],'requestId':request['requestId'],'state':'completed'})
        atomic_json(job/'result.json',{'scope':'SYSTEM_VERIFICATION_ONLY','value':'old result'})
        (job/'events.jsonl').write_text('')
        ledger=root/'jobs/requests';ledger.mkdir()
        atomic_json(ledger/(request['requestId']+'.json'),{'jobId':request['jobId'],'requestHash':digest})
        return {path:path.read_bytes() for path in [*job.iterdir(),ledger/(request['requestId']+'.json')]}

    def submit_fixture(self):
        from contextlib import ExitStack
        from types import SimpleNamespace
        from unittest.mock import patch
        from runtime import jobs
        stack=ExitStack();self.addCleanup(stack.close)
        launch=stack.enter_context(patch.object(jobs.subprocess,'Popen',return_value=SimpleNamespace(pid=999999999,wait=lambda:0)))
        stack.enter_context(patch.object(jobs,'_token',return_value='fixture-token'))
        stack.enter_context(patch.object(jobs,'inspect',side_effect=lambda root,id,*args,**kwargs:{'jobId':id,'state':'running'}))
        stack.enter_context(patch('runtime.simulation._cpu_environment',return_value={}))
        return launch

    def test_request_identity_is_scoped_by_project_and_generation_without_changing_same_scope_retries(self):
        launch=self.submit_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base={'requestId':'build_a','task':{'operation':'health'}}
            for index,(project,generation) in enumerate((('p_first','g_first'),('p_second','g_first'),('p_first','g_second'))):
                request={**base,'jobId':'j_'+str(index),'projectId':project,'generationId':generation}
                self.assertEqual(submit(root/'code',root,request)['jobId'],request['jobId'])
                self.assertEqual(submit(root/'code',root,{**request,'jobId':'alias_'+str(index)})['jobId'],request['jobId'])
                with self.assertRaisesRegex(ValueError,'different'):
                    submit(root/'code',root,{**request,'task':{'operation':'build'}})
            self.assertEqual(launch.call_count,3)
            self.assertEqual(len(list((root/'jobs/requests/v2').glob('*.json'))),3)

    def test_old_request_scope_is_read_from_its_original_payload_and_files_stay_unchanged(self):
        from unittest.mock import patch
        from runtime import jobs
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            request={'jobId':'old_job','requestId':'build_a','projectId':'p_old','generationId':'g_old','task':{'operation':'health'}}
            before=self.legacy_request(root,request)
            with patch.object(jobs.subprocess,'Popen') as launch:
                self.assertEqual(submit(root/'code',root,{**request,'jobId':'new_alias'})['jobId'],'old_job')
                self.assertEqual(submit(root/'code',root,request)['state'],'completed')
                launch.assert_not_called()
            self.assertEqual(len(list((root/'jobs/requests/v2').glob('*.json'))),1)
            with self.assertRaisesRegex(ValueError,'different'):
                submit(root/'code',root,{**request,'task':{'operation':'build'}})
            self.assertTrue(all(path.read_bytes()==data for path,data in before.items()))

    def test_a_foreign_legacy_receipt_does_not_reserve_another_scope(self):
        launch=self.submit_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            old={'jobId':'old_job','requestId':'build_a','projectId':'p_old','generationId':'g_old','task':{'operation':'health'}}
            before=self.legacy_request(root,old)
            request={**old,'jobId':'new_job','projectId':'p_new','generationId':'g_new'}
            self.assertEqual(submit(root/'code',root,request)['jobId'],'new_job')
            self.assertEqual(launch.call_count,1)
            self.assertTrue(all(path.read_bytes()==data for path,data in before.items()))

    def test_legacy_migration_can_be_retried_after_index_publication_failure_without_relaunching(self):
        from unittest.mock import patch
        from runtime import jobs
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            request={'jobId':'old_job','requestId':'r1','generationId':'g1','task':{'operation':'health'}}
            before=self.legacy_request(root,request);publish=jobs.atomic_json
            def interrupted(path,value):
                if Path(path).parent.name=='v2':raise OSError('fixture interrupted scoped index publication')
                return publish(path,value)
            with patch.object(jobs,'atomic_json',side_effect=interrupted),patch.object(jobs.subprocess,'Popen') as launch:
                with self.assertRaisesRegex(OSError,'interrupted'):submit(root/'code',root,request)
                launch.assert_not_called()
            with patch.object(jobs.subprocess,'Popen') as launch:
                self.assertEqual(submit(root/'code',root,request)['state'],'completed');launch.assert_not_called()
            self.assertTrue(all(path.read_bytes()==data for path,data in before.items()))

    def test_missing_legacy_scope_evidence_is_not_guessed_or_relaunched(self):
        from unittest.mock import patch
        from runtime import jobs
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            request={'jobId':'old_job','requestId':'r1','task':{'operation':'health'}}
            self.legacy_request(root,request);(root/'jobs/old_job/request.json').unlink()
            with patch.object(jobs.subprocess,'Popen') as launch:
                with self.assertRaisesRegex(ValueError,'legacy.*(scope|evidence|request)'):
                    submit(root/'code',root,request)
                launch.assert_not_called()

    def test_concurrent_same_scope_aliases_have_one_launch_and_one_durable_request(self):
        from concurrent.futures import ThreadPoolExecutor
        launch=self.submit_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            base={'requestId':'same','projectId':'p_one','generationId':'g_one','task':{'operation':'health'}}
            with ThreadPoolExecutor(max_workers=2) as pool:
                rows=list(pool.map(lambda index:submit(root/'code',root,{**base,'jobId':'j_'+str(index)}),range(2)))
            self.assertEqual(rows[0]['jobId'],rows[1]['jobId'])
            self.assertEqual(launch.call_count,1)
            self.assertEqual(len(list((root/'jobs/requests/v2').glob('*.json'))),1)

    def test_rejected_foreign_job_id_cannot_poison_scoped_alias_lookup(self):
        import json
        launch=self.submit_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            old={'jobId':'shared','requestId':'old_request','projectId':'p_old','generationId':'g_old','task':{'operation':'health'}}
            before=self.legacy_request(root,old)
            request={**old,'requestId':'new_request','projectId':'p_new','generationId':'g_new'}
            with self.assertRaisesRegex(ValueError,'different|scope|identity'):submit(root/'code',root,request)
            with self.subTest(step='rejected collision leaves no index'):
                self.assertEqual(list((root/'jobs/requests/v2').glob('*.json')),[])
            self.assertEqual(submit(root/'code',root,{**request,'jobId':'alias'})['jobId'],'alias')
            self.assertEqual(launch.call_count,1)
            actual=json.loads((root/'jobs/alias/request.json').read_text())['payload']
            self.assertEqual((actual['projectId'],actual['generationId']),('p_new','g_new'))
            self.assertTrue(all(path.read_bytes()==data for path,data in before.items()))

    def test_existing_scoped_index_does_not_override_actual_job_scope_digest_or_missing_evidence(self):
        import hashlib,json
        from unittest.mock import patch
        from runtime import jobs
        from runtime.records import atomic_json
        for failure in ('scope','digest','missing'):
            with tempfile.TemporaryDirectory() as directory,self.subTest(failure=failure):
                root=Path(directory)
                old={'jobId':'shared','requestId':'same_request','projectId':'p_old','generationId':'g_old','task':{'operation':'health'}}
                before=self.legacy_request(root,old)
                request={**old,'jobId':'alias'}
                if failure=='scope':request.update(projectId='p_new',generationId='g_new')
                elif failure=='digest':request['task']={'operation':'build'}
                else:
                    deleted=root/'jobs/shared/request.json';deleted.unlink();before.pop(deleted)
                scope=[request.get('projectId'),request.get('generationId'),request['requestId']]
                digest=hashlib.sha256(json.dumps({key:value for key,value in request.items() if key!='jobId'},sort_keys=True,allow_nan=False).encode()).hexdigest()
                scoped=root/'jobs/requests/v2';scoped.mkdir()
                index=scoped/(hashlib.sha256(json.dumps(scope,separators=(',',':')).encode()).hexdigest()+'.json')
                atomic_json(index,{'jobId':'shared','requestHash':digest,'scope':scope});original=index.read_bytes()
                with patch.object(jobs.subprocess,'Popen') as launch:
                    with self.assertRaisesRegex(ValueError,'scope|digest|evidence|different'):
                        submit(root/'code',root,request)
                    launch.assert_not_called()
                self.assertEqual(index.read_bytes(),original)
                self.assertTrue(all(path.read_bytes()==data for path,data in before.items()))


class InspectIdentityTests(unittest.TestCase):
    def scenario(self, status, current=None, *, completed_at_lock=False, actual_child_stop=False):
        import json
        from contextlib import contextmanager
        from unittest.mock import patch
        from runtime import jobs
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        job = root / 'jobs/j1'
        job.mkdir(parents=True)
        (job / 'status.json').write_text(json.dumps({'jobId': 'j1', **status}))
        (job / 'events.jsonl').write_text('')
        if status['state'] == 'completed':
            (job / 'result.json').write_text('{"passed":true}')
        before = {name: (job / name).read_bytes() for name in ('status.json', 'events.jsonl')}
        if (job / 'result.json').exists():
            before['result.json'] = (job / 'result.json').read_bytes()
        # Real stop helper sees an obsolete child token, and must never signal its replacement.
        (job / 'worker-process.json').write_text(json.dumps({'pid': 987654322, 'processToken': 'old-child', 'members': {}}))
        def token(pid):
            return 'replacement-child' if pid == 987654322 else current
        @contextmanager
        def terminal(_):
            if completed_at_lock:
                (job / 'status.json').write_text('{"jobId":"j1","state":"completed"}')
                (job / 'result.json').write_text('{"passed":true}')
            yield
        with patch.object(jobs, '_token', side_effect=token) as read, patch.object(jobs, 'terminal_lock', terminal), \
             patch.object(jobs, '_write_performance'), patch.object(jobs.os, 'killpg') as signal, patch('fcntl.flock'):
            if actual_child_stop:
                value = jobs.inspect(root, 'j1')
            else:
                with patch.object(jobs, '_stop_worker') as stop:
                    value = jobs.inspect(root, 'j1')
                    if value['state'] != 'failed':
                        stop.assert_not_called()
            signal.assert_not_called()
            reads = read.call_count
        return value, before, job, reads

    def test_missing_or_empty_recorded_identity_is_failed(self):
        for fields in ({'processToken': None}, {'processToken': ''}, {}, {'processToken': False}, {'processToken': 9}, {'processToken': []}):
            with self.subTest(fields=fields):
                value, _, _, _ = self.scenario({'state': 'running', 'pid': 987654321, **fields})
                self.assertEqual(value['state'], 'failed')
                self.assertIn('ended without a terminal receipt', value['error'])
        value, _, _, _ = self.scenario({'state': 'running', 'processToken': 'nonempty'})
        self.assertEqual(value['state'], 'failed')

    def test_nonempty_identity_is_live_only_while_current_token_matches(self):
        for current, expected in (('same', 'running'), (None, 'failed'), ('replacement', 'failed')):
            with self.subTest(current=current):
                value, _, _, _ = self.scenario({'state': 'running', 'pid': 987654321, 'processToken': 'same'}, current)
                self.assertEqual(value['state'], expected)

    def test_completed_does_not_read_identity_or_cleanup(self):
        value, before, job, reads = self.scenario({'state': 'completed'})
        self.assertEqual(value['state'], 'completed')
        self.assertEqual(value['result'], {'passed': True})
        self.assertEqual(reads, 0)
        for name, data in before.items():
            self.assertEqual((job / name).read_bytes(), data)

    def test_completed_published_while_waiting_for_lock_is_not_replaced(self):
        value, _, job, _ = self.scenario({'state': 'running', 'pid': 987654321, 'processToken': None}, completed_at_lock=True)
        self.assertEqual(value['state'], 'completed')
        self.assertEqual(value['result'], {'passed': True})
        self.assertEqual((job / 'events.jsonl').read_text(), '')

    def test_reused_child_identity_is_not_signalled_when_supervisor_identity_is_missing(self):
        value, _, _, _ = self.scenario({'state': 'running', 'pid': 987654321, 'processToken': None}, actual_child_stop=True)
        self.assertEqual(value['state'], 'failed')


class OwnerCleanupTests(unittest.TestCase):
    """Deterministic host-only faults. Fake children never prove Linux group/reap behavior."""
    def scenario(self,fail_write=1,*,cleanup_error=False,wait_error=False,terminal_error=False,natural=None,token_error=False,missing_token=False,expired=False):
        from contextlib import contextmanager
        import json,os,time
        from unittest.mock import patch,Mock
        from runtime import jobs
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);work=root/'work';work.mkdir();payload={'root':str(root),'operation':'check'}
        primary=OSError('actual owner Path.replace failed');cleanup=RuntimeError('cleanup injected failure');calls=[];held=[False];writes=[0];token_calls=[0];terminal_calls=[0]
        class Child:
            pid=12345;returncode=None
            def poll(self):
                if natural is not None:self.returncode=natural
                return self.returncode
            def wait(self,timeout=None):
                calls.append(('wait',timeout))
                if wait_error:raise RuntimeError('wait injected failure')
                if self.returncode is None:raise jobs.subprocess.TimeoutExpired('fake-child',timeout)
                return self.returncode
        child=Child()
        @contextmanager
        def terminal(directory):
            terminal_calls[0]+=1
            if terminal_error and terminal_calls[0]>1:raise OSError('terminal diagnostic injected failure')
            if held[0]:raise AssertionError('terminal lock reentered')
            held[0]=True
            try:yield
            finally:held[0]=False
        def token(pid):
            if pid==child.pid:
                token_calls[0]+=1
                if missing_token:return None
                if token_error and token_calls[0]==1:raise primary
                return 'own-token' if child.returncode is None else None
            return 'parent-token'
        def launch(*args,**kwargs):
            prefix={'schema':'sceneagent-performance-v1','requestHash':jobs.payload_digest(payload),'operation':'check',
                'worker':{'pid':child.pid,'processToken':'own-token'},'phase':'shutdown_started','inclusive':True,'incomplete':True,
                'groups':{},'shutdown':{'state':'started','startedCounter':time.perf_counter()}}
            jobs.atomic_json(work/'performance.json',prefix);return child
        def stop(identity,process):
            self.assertFalse(held[0],'cleanup must occur after terminal lock release')
            self.assertEqual(identity['pid'],child.pid);self.assertEqual(identity['processToken'],'own-token')
            calls.append(('stop',dict(identity)));child.returncode=0
            if cleanup_error:raise cleanup
        original=Path.replace
        def replace(source,target):
            if Path(target).name=='worker-process.json':
                writes[0]+=1
                if writes[0]==fail_write:raise primary
            return original(source,target)
        verify=Mock(return_value={'passed':True});caught=None
        original_read=Path.read_text
        def fake_proc_only(path,*args,**kwargs):
            if str(path)==f'/proc/{child.pid}/stat':raise FileNotFoundError('fake child has no OS process')
            return original_read(path,*args,**kwargs)
        def members(_):
            if child.returncode is not None:return {}
            value={child.pid:'own-token'}
            if writes[0]:value[23456]='new-child-token'
            return value
        with patch.object(Path,'read_text',fake_proc_only),patch.object(jobs,'terminal_lock',side_effect=terminal),patch('fcntl.flock'),patch.object(jobs.subprocess,'Popen',side_effect=launch), \
             patch.object(jobs,'_token',side_effect=token),patch.object(jobs,'_group_members',side_effect=members), \
             patch.object(jobs,'stop_process_group',side_effect=stop),patch.object(jobs,'verify_candidate',verify),patch.object(Path,'replace',replace), \
             patch.object(jobs.time,'monotonic',side_effect=([0.,1000.] if expired else None),wraps=None if expired else __import__('time').monotonic):
            try:jobs.supervise(payload,lambda *_:[],work,command=['fake-child'],timeout=.1)
            except BaseException as error:caught=error
        value=json.loads((work/'performance.json').read_text())
        return caught,primary,calls,verify,value,work,child

    def test_first_and_refresh_actual_atomic_replace_errors_cleanup_and_keep_primary(self):
        for index in (1,2):
            with self.subTest(index=index):
                error,primary,calls,verify,value,work,child=self.scenario(index)
                self.assertIs(error,primary);verify.assert_not_called();self.assertEqual(child.returncode,0)
                self.assertTrue(any(row[0]=='stop' for row in calls));self.assertIn(('wait',0),calls)
                self.assertEqual((work/'worker-process.json').exists(),index==2)
                self.assertNotIn('workerExitCode',value.get('parent',{}));self.assertNotIn('observedExitCounter',value.get('parent',{}))
                self.assertEqual(value['parent']['cleanupReturnCode'],0);self.assertIn('cleanupCompletedCounter',value['parent'])
                self.assertNotEqual(value['shutdown']['state'],'observed_exit');self.assertTrue(value['incomplete'])

    def test_cleanup_errors_are_notes_and_wait_is_still_attempted(self):
        for field in ('cleanup_error','wait_error'):
            with self.subTest(field=field):
                error,primary,calls,verify,value,_,_=self.scenario(**{field:True})
                self.assertIs(error,primary);verify.assert_not_called();self.assertIn(('wait',0),calls)
                self.assertTrue(any('injected failure' in note for note in error.__notes__))

    def test_missing_initial_identity_uses_only_raw_child_identity_and_retains_error(self):
        error,primary,calls,verify,value,_,_=self.scenario(fail_write=None,token_error=True)
        self.assertIs(error,primary);self.assertTrue(any(row[0]=='stop' for row in calls));verify.assert_not_called()
        self.assertIn(('wait',0),calls)

    def test_cleanup_failure_on_natural_return_cannot_be_success(self):
        error,_,calls,verify,value,_,_=self.scenario(fail_write=None,natural=0,cleanup_error=True)
        self.assertIsInstance(error,RuntimeError);self.assertIn('cleanup injected',str(error));verify.assert_called_once()
        self.assertEqual(value['parent']['workerExitCode'],0);self.assertIn('observedExitCounter',value['parent'])
        self.assertNotIn('cleanupReturnCode',value['parent'])


    def test_unknown_token_never_matches_none_or_claims_cleanup_completion(self):
        error,primary,calls,verify,value,_,child=self.scenario(missing_token=True)
        self.assertIsInstance(error,RuntimeError);self.assertIn('identity unavailable',str(error));verify.assert_not_called();self.assertIsNone(child.returncode)
        self.assertFalse(any(row[0]=='stop' for row in calls));self.assertIn(('wait',0),calls)
        self.assertNotIn('cleanupReturnCode',value['parent']);self.assertNotIn('cleanupCompletedCounter',value['parent'])
        self.assertTrue(any('unconfirmed' in note for note in error.__notes__))

    def test_terminal_diagnostic_error_is_a_note_after_cleanup(self):
        error,primary,calls,verify,_,_,_=self.scenario(terminal_error=True)
        self.assertIs(error,primary);self.assertIn(('wait',0),calls);verify.assert_not_called()
        self.assertTrue(any('terminal diagnostic' in note for note in error.__notes__))

    def test_owner_io_failure_crossing_deadline_keeps_first_error(self):
        error,primary,_,verify,_,_,_=self.scenario(fail_write=2,expired=True)
        self.assertIs(error,primary);self.assertNotIsInstance(error,TimeoutError);verify.assert_not_called()

    def test_no_child_created_for_popen_failure_or_prior_cancel(self):
        from unittest.mock import patch
        from runtime import jobs
        import json
        for mode in ('popen','cancel'):
            temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);root=Path(temporary.name);work=root/'work';work.mkdir()
            if mode=='cancel':(work/'status.json').write_text(json.dumps({'state':'cancelled'}))
            primary=OSError('Popen failed')
            with patch('fcntl.flock'),patch.object(jobs.subprocess,'Popen',side_effect=primary) as popen,patch.object(jobs,'stop_process_group') as stop:
                with self.assertRaises((OSError,RuntimeError)) as caught:jobs.supervise({'root':str(root),'operation':'check'},lambda *_:[],work,['fake'])
                stop.assert_not_called()
                if mode=='popen':self.assertIs(caught.exception,primary)
                else:popen.assert_not_called()


@unittest.skipUnless(__import__('sys').platform == 'linux', 'requires separately scheduled real Linux CPU process group')
class OwnerCleanupLinuxTests(unittest.TestCase):
    def run_fault(self,index,after_stop_error=False):
        import json,os,signal,subprocess,sys,time
        from unittest.mock import Mock,patch
        from runtime import jobs
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);root=Path(temporary.name);work=root/'work';work.mkdir()
        child=root/'child.py'
        child.write_text("""import pathlib,signal,subprocess,sys,time
sys.path.insert(0,sys.argv[1])
from runtime import jobs
work=pathlib.Path(sys.argv[2]);payload=__import__('json').loads((work/'worker-request.json').read_text())
signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
companion=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
jobs.start_performance(work,payload);jobs.shutdown_started(True)
(work/'ready.json').write_text(__import__('json').dumps({'pid':__import__('os').getpid(),'companion':companion.pid}))
time.sleep(30)
""")
        primary=OSError('real owner atomic replace failed');replace=Path.replace;created=[];popen=jobs.subprocess.Popen;writes=[0];stop=jobs.stop_process_group
        def launch(*args,**kwargs):
            process=popen(*args,**kwargs);identity={'pid':process.pid,'processToken':jobs._token(process.pid),'members':{}}
            created.append((process,identity))
            evidence=os.environ.get('JOB_OWNER_CPU_EVIDENCE')
            if evidence:
                directory=Path(evidence);directory.mkdir(parents=True,exist_ok=True)
                (directory/f'created-owner-{index}-stoperror-{after_stop_error}.json').write_text(json.dumps({'scope':'test-created raw Popen only',
                    'identity':identity,'root':str(root),'jobsSource':str(Path(jobs.__file__).resolve())},indent=2)+'\n')
            return process
        def fault(source,target):
            if Path(target).name=='worker-process.json':
                writes[0]+=1
                if writes[0]==index:
                    if index==2:
                        deadline=time.monotonic()+5
                        while not (work/'ready.json').exists() and time.monotonic()<deadline:time.sleep(.01)
                        if not (work/'ready.json').exists():raise RuntimeError('isolated CPU child did not become ready')
                    raise primary
            return replace(source,target)
        def stop_then_error(identity,process=None):
            stop(identity,process)
            if after_stop_error:raise RuntimeError('after actual group stop injected error')
        verify=Mock(side_effect=AssertionError('owner I/O failure must not verify'))
        try:
            with patch.object(jobs.subprocess,'Popen',side_effect=launch),patch.object(Path,'replace',fault), \
                 patch.object(jobs,'stop_process_group',side_effect=stop_then_error),patch.object(jobs,'verify_candidate',verify):
                with self.assertRaises(OSError) as caught:
                    jobs.supervise({'root':str(root),'operation':'check'},lambda *_:[],work,
                        [sys.executable,str(child),str(Path(jobs.__file__).parent.parent),str(work)],timeout=10)
            self.assertIs(caught.exception,primary);verify.assert_not_called();self.assertEqual(len(created),1)
            process,identity=created[0];self.assertIsNotNone(process.returncode);self.assertEqual(process.wait(timeout=0),process.returncode)
            self.assertEqual(jobs._group_members(process.pid),{})
            self.assertEqual((work/'worker-process.json').exists(),index==2)
            value=json.loads((work/'performance.json').read_text());self.assertNotIn('workerExitCode',value['parent']);self.assertNotIn('observedExitCounter',value['parent'])
            self.assertEqual(value['parent']['cleanupReturnCode'],process.returncode);self.assertTrue(value['incomplete'])
            self.assertNotEqual(value['shutdown']['state'],'observed_exit')
            if index==2:self.assertEqual(process.returncode,0)
            if after_stop_error:self.assertTrue(any('after actual group stop' in note for note in primary.__notes__))
            evidence=__import__('os').environ.get('JOB_OWNER_CPU_EVIDENCE')
            if evidence:
                directory=Path(evidence);directory.mkdir(parents=True,exist_ok=True)
                (directory/f'owner-{index}-stoperror-{after_stop_error}.json').write_text(json.dumps({'purpose':'isolated real Linux CPU fixture',
                    'jobsSource':str(Path(jobs.__file__).resolve()),'jobsSha256':__import__('hashlib').sha256(Path(jobs.__file__).read_bytes()).hexdigest(),
                    'replaceFailureIndex':index,'firstError':str(primary),'firstErrorPreserved':True,'actualChildExit':process.returncode,
                    'directWaitReaped':True,'liveGroupMembers':jobs._group_members(process.pid),'identity':identity,
                    'ownerFileExists':(work/'worker-process.json').exists(),'parentPerformance':value['parent'],'notes':getattr(primary,'__notes__',[])},indent=2)+'\n')
        finally:
            # Independent red-test safety: only handles created by this test, never a persisted owner file.
            for process,identity in created:
                stop(identity,process);code=process.wait(timeout=5)
                evidence=os.environ.get('JOB_OWNER_CPU_EVIDENCE')
                if evidence:
                    directory=Path(evidence);directory.mkdir(parents=True,exist_ok=True)
                    (directory/f'independent-reap-owner-{index}-stoperror-{after_stop_error}.json').write_text(json.dumps({'identity':identity,
                        'actualChildExit':code,'directWaitReturned':True,'liveGroupMembers':jobs._group_members(process.pid)},indent=2)+'\n')

    def test_first_real_owner_replace_failure_reaps_created_group(self):self.run_fault(1)
    def test_refresh_replace_failure_term_zero_stays_cleanup_only(self):self.run_fault(2)
    def test_after_real_stop_error_preserves_owner_primary_and_reap(self):self.run_fault(2,after_stop_error=True)


if __name__ == '__main__':
    unittest.main()
