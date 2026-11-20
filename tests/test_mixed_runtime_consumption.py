"""SYSTEM_VERIFICATION_ONLY: frozen consumer dispatch; fake reports are never product evidence."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from runtime import worker


class MixedRuntimeConsumptionTests(unittest.TestCase):
    def fixture(self, root):
        project = root/'projects/p'
        refs, episodes, cases = [], [], []
        for index in range(2):
            revision = 'r'+str(index)
            source = '# SYSTEM_VERIFICATION_ONLY frozen runtime '+str(index)+'\n'
            code = hashlib.sha256(b'worker.py'+source.encode()).hexdigest()
            snapshot = root/'runtime'/code/'runtime'; snapshot.mkdir(parents=True)
            (snapshot/'worker.py').write_text(source)
            directory = project/'revisions'/revision; directory.mkdir(parents=True)
            manifest = {'schema':'sceneagent-build-v6','engine':'isaacsim','physics':'physx','status':'completed',
                'projectId':'p','revision':revision,'runtimeSnapshotRef':'runtime/'+code,
                'runtimeExecution':{'python':sys.executable,'prefix':sys.prefix},'runtime':'key'+str(index),
                'artifacts':{'manifest':str((directory/'manifest.json').relative_to(root))},
                'tasks':[{'id':'task'}]}
            worker.save(directory/'manifest.json',manifest)
            refs.append({'revision':revision,'split':'train','taskIds':['task']})
            episodes.append({'revision':revision,'episodeId':'demo'+str(index),'taskId':'task','use':'execution_data'})
            case = 'case'+str(index); cases.append(case)
            (project/'episodes'/case).mkdir(parents=True)
            worker.save(project/'episodes'/case/'manifest.json',{'revision':revision,'taskId':'task'})
        payload = {'operation':'consume','root':str(root),'projectId':'p','revision':'r0','requestId':'mixed',
            'requiredOutputs':['generation','demonstrations','evaluation'],'environmentRefs':refs,
            'episodes':episodes,'validationEpisodes':cases,'workerTimeoutSeconds':123}
        self.enterContext(patch.object(worker,'validate_publication'))
        return payload

    def test_different_frozen_versions_dispatch_before_current_runtime_load(self):
        from runtime import consumption
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();payload=self.fixture(root)
            with patch.object(worker,'frozen',side_effect=ValueError('运行内核已变化，需要重新构建场景')), \
                 patch.object(consumption,'consume',return_value={'routed':True}) as route:
                self.assertEqual(worker.consume_internal(payload,root),{'routed':True})
            self.assertEqual(route.call_count,1)

    def test_frozen_rollout_uses_its_original_worker_with_the_same_request(self):
        from runtime import jobs
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            source = self.fixture(root)
            device = {'index': 0, 'uuid': 'GPU-10000000-0000-0000-0000-000000000000'}
            payload = {**source, 'operation': 'rollout', 'device': device,
                       'robotSetup': {'robots': [{'id': 'arm'}]}}
            manifest = json.loads((root/'projects/p/revisions/r0/manifest.json').read_text())
            work = root/'job'
            with patch.object(jobs, 'assign_device', return_value='570.211.01'), \
                 patch.object(jobs.subprocess, 'Popen', side_effect=OSError('stop before SDK')) as launch:
                with self.assertRaisesRegex(OSError, 'stop before SDK'):
                    jobs.supervise(payload, lambda *_: [], work)
            command = launch.call_args.args[0]
            self.assertEqual(command[:2], [sys.executable,
                str(root/manifest['runtimeSnapshotRef']/'runtime/worker.py')])
            self.assertEqual(json.loads((work/'worker-request.json').read_text()), payload)

    def test_frozen_rollout_rejects_changed_kernel_before_starting_kit(self):
        from runtime import jobs
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            source = self.fixture(root)
            payload = {**source, 'operation': 'rollout'}
            manifest = json.loads((root/'projects/p/revisions/r0/manifest.json').read_text())
            (root/manifest['runtimeSnapshotRef']/'runtime/worker.py').write_text('changed')
            with patch.object(jobs.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'snapshot digest mismatch'):
                    jobs.supervise(payload, lambda *_: [], root/'job')
                launch.assert_not_called()

    def test_successful_frozen_rollout_is_checked_by_its_original_verifier(self):
        from unittest.mock import Mock
        from runtime import consumption, jobs
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            source = self.fixture(root)
            device = {'index': 0, 'uuid': 'GPU-10000000-0000-0000-0000-000000000000'}
            payload = {**source, 'operation': 'rollout', 'device': device}
            work = root/'job'
            child = Mock(pid=12345)
            child.poll.return_value = child.wait.return_value = 0
            with patch.object(jobs, 'assign_device', return_value='570.211.01'), \
                 patch.object(jobs.subprocess, 'Popen', return_value=child), \
                 patch.object(jobs, '_token', return_value='own-token'), \
                 patch.object(jobs, '_group_members', return_value={}), \
                 patch.object(jobs, 'stop_process_group'), \
                 patch.object(jobs, 'verify_candidate') as current, \
                 patch.object(consumption, 'verify_frozen_candidate', return_value=({'success': True}, set())) as original:
                self.assertEqual(jobs.supervise(payload, lambda *_: [], work), {'success': True})
            current.assert_not_called()
            self.assertEqual(original.call_args.args[1:], (work, payload, 0))

    def test_partition_preserves_exact_selection_and_original_interpreter(self):
        from runtime import consumption
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();payload=self.fixture(root)
            groups=consumption.plan(payload,root)
            self.assertEqual(len(groups),2)
            for index,group in enumerate(groups):
                request=group['payload']
                self.assertEqual(request['environmentRefs'],[payload['environmentRefs'][index]])
                self.assertEqual(request['episodes'],[payload['episodes'][index]])
                self.assertEqual(request['validationEpisodes'],[payload['validationEpisodes'][index]])
                self.assertEqual(request['revision'],'r'+str(index))
                self.assertEqual(group['runtimeExecution']['python'],sys.executable)
                self.assertEqual(request['workerTimeoutSeconds'],123)
            self.assertNotEqual(groups[0]['payload']['requestId'],groups[1]['payload']['requestId'])

    def test_dispatch_rejects_tampering_and_out_of_scope_records_before_running(self):
        from runtime import consumption
        for corruption in ('code','symlink','primary','demo','case','duplicate_case','publication'):
            with self.subTest(corruption=corruption),tempfile.TemporaryDirectory() as name:
                root=Path(name).resolve();payload=self.fixture(root)
                if corruption in ('code','symlink'):
                    path=next((root/'runtime').glob('*/runtime/worker.py'))
                    if corruption=='code':path.write_text('changed')
                    else:
                        value=path.read_text();path.unlink();target=root/'external.py';target.write_text(value);path.symlink_to(target)
                if corruption=='primary':payload['revision']='foreign'
                if corruption=='demo':payload['episodes'][0]['revision']='foreign'
                if corruption=='case':worker.save(root/'projects/p/episodes/case0/manifest.json',{'revision':'foreign','taskId':'task'})
                if corruption=='duplicate_case':payload['validationEpisodes'].append('case0')
                if corruption=='publication':worker.validate_publication.side_effect=ValueError('publication mismatch')
                with self.assertRaises(ValueError):consumption.plan(payload,root)

    def reports(self, root, payload):
        from runtime import consumption
        groups=consumption.plan(payload,root);results=[]
        for group in groups:
            request=group['payload'];revision=request['revision']
            report={'schema':'sceneagent-product-consumption-v1','passed':True,'projectId':'p','revision':revision,
                'requiredOutputs':request['requiredOutputs'],'environmentRefs':request['environmentRefs'],
                'validationEpisodes':request['validationEpisodes'],'limitations':['fixture only'],
                'demonstrations':{'passed':True,'episodeCount':1,'batches':[request['episodes'][0]]},
                'reload':{'passed':True,'newProcess':True,'originalProjectDirectoryRequired':False,
                          'environments':[{'revision':revision,'passed':True}]},
                'evaluation':{'passed':True,'episodeCount':1,'tasks':[{'revision':revision,'taskId':'task','passed':True}],
                    'interfaces':[{'revision':revision,'taskId':'task','passed':True}],
                    'isolation':{'passed':True,'checks':{revision+':task:networkBlocked':True},
                                 'mechanism':'bubblewrap_unshare_all_explicit_dependency_mounts'}}}
            ref='projects/p/consumer-reports/'+request['requestId']+'/report.json'
            (root/ref).parent.mkdir(parents=True)
            worker.save(root/ref,report);results.append({**report,'artifactRef':ref})
        return groups,results

    def test_aggregation_rechecks_original_children_and_rejects_rewritten_summary(self):
        from runtime import consumption
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();payload=self.fixture(root);groups,reports=self.reports(root,payload)
            def child(group,work,execute):
                index=groups.index(group)
                return reports[index],{root/reports[index]['artifactRef']}
            with patch.object(consumption,'native',side_effect=child) as run:
                result=consumption.consume(payload,root)
                self.assertEqual(result['environmentRefs'],payload['environmentRefs'])
                self.assertEqual(result['demonstrations']['episodeCount'],2)
                self.assertEqual(len(result['evaluation']['interfaces']),2)
                self.assertTrue(result['passed'])
                required=consumption.consume(payload,root,result)
                self.assertIn(root/result['artifactRef'],required)
                self.assertEqual([call.args[2] for call in run.call_args_list],[True,True,False,False])
                changed=deepcopy(result);changed['evaluation']['interfaces'][0]['passed']=False
                worker.save(root/result['artifactRef'],{k:v for k,v in changed.items() if k!='artifactRef'})
                with self.assertRaises(ValueError):consumption.consume(payload,root,changed)
            with patch.object(consumption,'native',side_effect=ValueError('original child evidence changed')):
                worker.save(root/result['artifactRef'],{k:v for k,v in result.items() if k!='artifactRef'})
                with self.assertRaisesRegex(ValueError,'original child'):consumption.consume(payload,root,result)

    @unittest.skipUnless(sys.platform=='linux','actual Linux process-group ownership')
    def test_cancelling_outer_worker_terminates_original_kernel_and_its_child(self):
        from runtime import jobs
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();snapshot=root/'runtime/fake/runtime';snapshot.mkdir(parents=True)
            source="import json,os,subprocess,sys,time\nfrom pathlib import Path\nchild=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\nPath(sys.argv[2]).joinpath('pids.json').write_text(json.dumps([os.getpid(),child.pid]))\ntime.sleep(120)\n"
            (snapshot/'worker.py').write_text(source)
            group={'runtimeSnapshotRef':'runtime/fake','runtimeExecution':{'python':sys.executable},
                'payload':{'root':str(root),'operation':'consume','workerTimeoutSeconds':90}}
            program='import json,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom runtime.consumption import native\nnative(json.loads(sys.argv[2]),Path(sys.argv[3]),True)'
            process=subprocess.Popen([sys.executable,'-B','-c',program,str(Path(worker.__file__).resolve().parents[1]),json.dumps(group),str(root/'child')],
                start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            identity={'pid':process.pid,'processToken':jobs._token(process.pid),'members':{}}
            try:
                deadline=time.monotonic()+10
                while not (root/'child/pids.json').exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
                pids=json.loads((root/'child/pids.json').read_text())
                self.assertTrue(all(os.getpgid(pid)==process.pid for pid in pids))
                tokens={pid:jobs._token(pid) for pid in pids}
                jobs.stop_process_group(identity,process);process.wait(timeout=5)
                self.assertTrue(all(jobs._token(pid)!=token for pid,token in tokens.items()))
            finally:
                jobs.stop_process_group(identity,process);process.wait(timeout=5)

    def test_failed_original_exit_preserves_attempt_and_cannot_be_reused_as_success(self):
        from runtime import consumption
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();snapshot=root/'runtime/fake/runtime';snapshot.mkdir(parents=True)
            (snapshot/'worker.py').write_text("import sys\nprint('SYSTEM_VERIFICATION_ONLY deliberate failure')\nsys.exit(7)\n")
            group={'runtimeSnapshotRef':'runtime/fake','runtimeExecution':{'python':sys.executable},
                'payload':{'root':str(root),'operation':'consume','workerTimeoutSeconds':10}}
            directory=root/'child'
            with self.assertRaisesRegex(ValueError,'did not exit successfully'):consumption.native(group,directory,True)
            self.assertEqual(json.loads((directory/'exit.json').read_text())['exitCode'],7)
            self.assertIn('deliberate failure',(directory/'kit.log').read_text())
            with self.assertRaisesRegex(ValueError,'did not exit successfully'):consumption.native(group,directory,False)
            with self.assertRaises(FileExistsError):consumption.native(group,directory,True)


if __name__=='__main__':unittest.main()
