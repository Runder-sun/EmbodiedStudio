"""SYSTEM_VERIFICATION_ONLY: trusted MDL consumer process boundaries, no SDK/GPU."""
from dataclasses import asdict
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import runtime
from runtime import worker
from runtime.scene import Task, World


class MdlConsumerProcessTests(unittest.TestCase):
    def fixture(self, root, revision='r1', *, mdl=True):
        project = root/'projects/p'
        directory = project/'revisions'/revision
        directory.mkdir(parents=True)
        audit = {'schema':'sceneagent-native-stage-audit-v2' if mdl else 'sceneagent-native-stage-audit-v1',
                 'files':{}, **({'mdl':{'fixture':True}} if mdl else {})}
        worker.save(directory/'native-stage-audit.json', audit)
        world = World(native=True)
        task = Task('t')
        worker.save(directory/'scene.json', world.snapshot())
        manifest = {'projectId':'p', 'revision':revision, 'compiled':{'nativeStageAudit':'native-stage-audit.json'},
                    'frozenArtifactDigests':{'native-stage-audit.json':worker.digest(directory/'native-stage-audit.json')},
                    'tasks':[asdict(task)]}
        worker.save(directory/'manifest.json', manifest)
        selected = {'sourceRef':'interface_probe.py', 'sourceFiles':{'interface_probe.py':'SYSTEM_VERIFICATION_ONLY'},
                    'sourceDigest':'fixture', 'helperDigests':{}}
        payload = {'operation':'consume', 'root':str(root), 'projectId':'p', 'revision':revision,
                   'requestId':'consume', 'purpose':'consumer_diagnostic', 'workerTimeoutSeconds':123}
        output = project/'consumer-reports/consume/interfaces'/revision/'t/episodes'
        output.parent.mkdir(parents=True)
        return project,directory,manifest,world,task,selected,payload,output

    def summary(self, root, output, revision='r1'):
        return {'projectId':'p', 'revision':revision, 'taskId':'t',
                'artifacts':{'manifest':str((output/'episode/manifest.json').relative_to(root))}}

    def test_two_mdl_roots_dispatch_to_distinct_trusted_sdk_processes(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            first = self.fixture(root)
            second = self.fixture(root, 'r2')
            requests = []
            def child(command, **options):
                self.assertEqual(command[:3], [sys.executable, str(Path(worker.__file__).resolve()), '--native-interface-episode'])
                self.assertNotIn('bwrap', command)
                self.assertNotIn('--kit-worker', command)
                self.assertTrue(options['close_fds'])
                self.assertEqual(options['timeout'], 123)
                request = json.loads(options['input'])
                requests.append(request)
                output = Path(request['outputParent'])
                packet = {'episode':self.summary(root,output,request['manifest']['revision']),
                          'termination':{'applicationStarted':True,'terminationMode':'official_fast_shutdown',
                                         'replicatorCompleted':True,'fullVendorCleanupVerified':False}}
                worker.save(Path(command[3]), packet)
                options['stdout'].write(b'SYSTEM_VERIFICATION_ONLY child SDK log\n')
                return SimpleNamespace(returncode=0)
            with patch.object(worker, '_episode', side_effect=AssertionError('MDL must not reuse parent SDK')), \
                 patch.object(worker.subprocess, 'run', side_effect=child):
                for project,directory,manifest,world,task,selected,payload,output in (first, second):
                    result = worker._consumer_interface_episode(root,project,directory,manifest,world,task,selected,
                        'candidate',payload,output_parent=output,interface_probes=[])
                    self.assertEqual(result['revision'],manifest['revision'])
            self.assertEqual([request['directory'] for request in requests], [str(first[1]),str(second[1])])
            self.assertEqual([request['parent']['pid'] for request in requests], [os.getpid(),os.getpid()])

    def test_plain_usd_keeps_existing_direct_episode(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            project,directory,manifest,world,task,selected,payload,output = self.fixture(root, mdl=False)
            with patch.object(worker, '_episode', return_value={'direct':True}) as direct, \
                 patch.object(worker.subprocess, 'run', side_effect=AssertionError('no extra process for plain USD')):
                result = worker._consumer_interface_episode(root,project,directory,manifest,world,task,selected,
                    'candidate',payload,output_parent=output,interface_probes=[])
            self.assertEqual(result,{'direct':True})
            self.assertEqual(direct.call_args.args[2], directory)

    def test_result_file_before_failed_sdk_exit_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            project,directory,manifest,world,task,selected,payload,output = self.fixture(root)
            def child(command, **options):
                worker.save(Path(command[3]), {'episode':self.summary(root,output),
                    'termination':{'applicationStarted':True,'terminationMode':'official_fast_shutdown',
                                   'replicatorCompleted':True,'fullVendorCleanupVerified':False}})
                return SimpleNamespace(returncode=7)
            with patch.object(worker.subprocess, 'run', side_effect=child), self.assertRaisesRegex(ValueError,'SDK|interface'):
                worker._consumer_interface_episode(root,project,directory,manifest,world,task,selected,
                    'candidate',payload,output_parent=output,interface_probes=[])

    def test_mdl_discovery_checks_secondary_revision_without_importing_usd(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            self.fixture(root, mdl=False)
            self.fixture(root, 'r2', mdl=True)
            payload = {'operation':'consume', 'root':str(root),'projectId':'p','revision':'r1',
                       'environmentRefs':[{'revision':'r1'},{'revision':'r2'}]}
            with patch.dict(sys.modules, {'pxr':None}), patch.object(worker,'frozen',side_effect=AssertionError('pure JSON preflight')):
                self.assertTrue(worker.consume_uses_mdl(payload))
                self.assertFalse(worker.consume_uses_mdl({**payload,'environmentRefs':[{'revision':'r1'}]}))

    def test_mdl_preflight_rejects_a_changed_audit_before_dispatch(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            self.fixture(root)
            path = root/'projects/p/revisions/r1/native-stage-audit.json'
            path.write_text('{}')
            with self.assertRaisesRegex(ValueError,'audit|frozen'):
                worker.consume_uses_mdl({'operation':'consume','root':str(root),'projectId':'p','revision':'r1'})

    def test_parent_sdk_is_deferred_when_a_secondary_environment_has_mdl(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            self.fixture(root,mdl=False);self.fixture(root,'r2',mdl=True)
            payload = {'operation':'consume','root':str(root),'projectId':'p','revision':'r1',
                       'environmentRefs':[{'revision':'r1'},{'revision':'r2'}]}
            directory = root/'jobs/consume';directory.mkdir(parents=True)
            worker.save(directory/'worker-parent.json',{'pid':os.getppid(),'processToken':'fixture'})
            worker.save(directory/'worker-request.json',payload)
            sdk = ModuleType('runtime.simulation');calls=[]
            sdk._ensure_app = lambda: (_ for _ in ()).throw(AssertionError('do not bind parent to the primary roots'))
            sdk.finish = lambda: {'applicationStarted':False,'terminationMode':'no_sdk_application'}
            sdk.shutdown = lambda code: (_ for _ in ()).throw(SystemExit(code))
            with patch.dict(sys.modules,{'runtime.simulation':sdk,'pxr':None}), \
                 patch.object(runtime,'simulation',sdk,create=True),patch.object(worker.sys,'platform','darwin'), \
                 patch('runtime.jobs._token',return_value='fixture'), \
                 patch.object(worker,'run',side_effect=lambda *args,**options:calls.append('run') or {}), \
                 patch.object(worker,'required_files',return_value=set()),patch('runtime.jobs.write_candidate'):
                with self.assertRaises(SystemExit) as exited:worker.kit_entry(directory)
            self.assertEqual(exited.exception.code,0)
            self.assertEqual(calls,['run'])

    def test_scoped_consumer_dispatches_each_mdl_environment_and_reverification_never_replays(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            first = self.fixture(root)
            second = self.fixture(root,'r2')
            for fixture in (first,second):
                fixture[2].update(assets={},io={'ports':{}})
            contexts = {row[2]['revision']:row for row in (first,second)}
            payload = {**first[6], 'requiredOutputs':['generation','evaluation'],
                       'environmentRefs':[{'revision':'r1'},{'revision':'r2'}], 'validationEpisodes':[]}
            # Only _consume_scoped owns this request directory in this fixture.
            shutil.rmtree(first[0]/'consumer-reports',ignore_errors=True)
            def frozen(_, request):
                context = contexts[request['revision']]
                return context[:4]
            def case_source(_,project,directory,manifest,world,task_id,ids,**options):
                return {'revision':manifest['revision'],'taskId':task_id}, {'transitions':[{'executedAction':{}}]}
            def fresh(*args,**options):
                return self.summary(root,options['output_parent'],args[3]['revision'])
            def evidence(root,directory,manifest,world,output,source,batch,episode_ref,**options):
                return {'passed':True,'revision':manifest['revision'],'taskId':'t','episodeRef':episode_ref},set(),{'networkBlocked':True}
            with ExitStack() as stack:
                stack.enter_context(patch.object(worker,'frozen',side_effect=frozen))
                stack.enter_context(patch.object(worker,'validation_cases',return_value=[{'passed':True}]))
                stack.enter_context(patch.object(worker,'_case_interface_source',side_effect=case_source))
                stack.enter_context(patch.object(worker,'_interface_source',return_value=first[5]))
                stack.enter_context(patch.object(worker,'_interface_evidence',side_effect=evidence))
                dispatch = stack.enter_context(patch.object(worker,'_consumer_interface_episode',side_effect=fresh))
                stack.enter_context(patch.object(worker,'_episode',side_effect=AssertionError('do not replay in shared parent')))
                result = worker._consume_scoped(payload,root)
                self.assertEqual(dispatch.call_count,2)
                self.assertEqual({row['revision'] for row in result['evaluation']['interfaces']},{'r1','r2'})
                worker._consume_scoped(payload,root,result)
                self.assertEqual(dispatch.call_count,2)

    def test_no_sdk_or_incomplete_finish_packet_is_not_an_interface_barrier(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)/'native-interface-result.json'
            for termination in ({'applicationStarted':False,'terminationMode':'no_sdk_application'},
                                {'applicationStarted':True,'terminationMode':'official_fast_shutdown'}):
                worker.save(path,{'episode':{},'termination':termination})
                with self.subTest(termination=termination),self.assertRaisesRegex(ValueError,'termination barrier'):
                    worker._interface_sdk_packet(path)

    def test_child_rebinds_isolation_probes_to_its_real_host_pid(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            project,directory,manifest,world,task,selected,payload,output = self.fixture(root)
            result_file = output.parent/'native-interface-result.json'
            request = {'root':str(root),'project':str(project),'directory':str(directory),'manifest':manifest,
                'world':world.snapshot(),'task':asdict(task),'selected':selected,'kind':'candidate','payload':payload,
                'outputParent':str(output),'interfaceProbes':[
                    ['parentMemoryBlocked','/proc/999/mem',False],
                    ['parentDescriptorsBlocked','/proc/999/fd/0',False],
                    ['outputReadBlocked','/private-score.json',False]],
                'parent':{'pid':123,'processToken':'fixture'}}
            sdk = ModuleType('runtime.simulation')
            sdk.finish = lambda:{'applicationStarted':True,'terminationMode':'official_fast_shutdown',
                                'replicatorCompleted':True,'fullVendorCleanupVerified':False}
            sdk.shutdown = lambda code: (_ for _ in ()).throw(SystemExit(code))
            def episode(*args, **options):
                probes = {name:path for name,path,_ in options['interface_probes']}
                self.assertEqual(probes['parentMemoryBlocked'],f'/proc/{os.getpid()}/mem')
                self.assertEqual(probes['parentDescriptorsBlocked'],f'/proc/{os.getpid()}/fd/0')
                self.assertEqual(probes['outputReadBlocked'],'/private-score.json')
                return self.summary(root,output)
            with patch.object(worker.sys,'stdin',io.StringIO(json.dumps(request))), \
                 patch.object(worker.sys,'platform','darwin'), patch.object(worker.os,'getppid',return_value=123), \
                 patch('runtime.jobs._token',return_value='fixture'), patch.object(worker,'verify_inputs',return_value=set()), \
                 patch.object(worker,'runtime_key',return_value='fixture'), patch.object(worker,'_episode',side_effect=episode), \
                 patch.dict(sys.modules,{'runtime.simulation':sdk}), patch.object(runtime,'simulation',sdk,create=True):
                with self.assertRaises(SystemExit) as exited:
                    worker.native_interface_entry(result_file)
            self.assertEqual(exited.exception.code,0)
            self.assertTrue(json.loads(result_file.read_text())['termination']['applicationStarted'])


if __name__ == '__main__':
    unittest.main()
