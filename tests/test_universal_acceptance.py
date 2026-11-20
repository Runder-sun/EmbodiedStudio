"""SYSTEM_VERIFICATION_ONLY CPU interfaces; no simulator or product-quality claims."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

from runtime import worker
from tests import test_frozen_evaluation as frozen_fixtures
from tests.test_frozen_evaluation import HOLD


class UniversalAcceptanceTests(unittest.TestCase):
    def test_runtime_identity_supports_a_source_install_without_a_planner_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            entry = runtime/'worker.py'
            entry.write_text('# SYSTEM_VERIFICATION_ONLY\n')
            with patch.object(worker, '__file__', str(entry)), patch.object(worker, 'runtime_versions', return_value={}):
                without_lock = worker.runtime_key()
                (runtime/'motion-dependencies.json').write_text('{}')
                self.assertNotEqual(worker.runtime_key(), without_lock)

    def test_new_contract_loads_the_exported_environment_root_and_rejects_missing_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root/'projects/p1'; project.mkdir(parents=True)
            exported = project/'deliveries/artifacts'/('a'*64)/'environment'
            snapshot = exported/'runtime/frozen/runtime'; snapshot.mkdir(parents=True)
            runtime = Path(worker.__file__).parent
            paths = [*runtime.glob('*.py'), *runtime.glob('motion-dependencies.json')]
            for source in paths: shutil.copyfile(source, snapshot/source.name)
            manifest = {'runtimeSnapshotRef':'runtime/frozen', 'assets':{}}
            payload = {'projectId':'p1', 'revision':'rev1', 'environmentRoot':str(exported.relative_to(root))}
            expected = (project, exported/'projects/p1/revisions/rev1', manifest, 'exported-world')
            with patch.object(worker, 'frozen', return_value=expected) as reader:
                actual = worker._consumed_environment(root, payload, manifest)
                self.assertEqual(actual[1:], expected[1:])
                self.assertEqual(reader.call_args.args[0], exported)
                (snapshot/'worker.py').unlink()
                with self.assertRaises(ValueError): worker._consumed_environment(root, payload, manifest)

    def fixture(self, root, *, outcome=False, visible_array=False, array_details=False):
        fixture = frozen_fixtures.FrozenEvaluationTests()
        self.addCleanup(fixture.doCleanups)
        project, directory, manifest, world = fixture.context(root)
        if visible_array or array_details:
            from tests.test_native_records import IO
            import numpy as np
            manifest['io']['streams']['points']=deepcopy(IO['streams']['nodes'])
            manifest['io']['streams']['points']['visibleToPolicy']=True
            manifest['compiled']['io']=deepcopy(manifest['io'])
            fixture._system_fixture_streams['points']=np.arange(168*3,dtype=np.float32).reshape(168,3)
        if outcome == 'terminal':
            rubric = "def evaluate(task,facts,state):\n    sample=facts['initialSnapshot']['truth'] if facts['phase']=='reset' else facts['truthAfter']\n    return {'state':None,'result':{'success':False,'terminated':bool(sample['streams']['q'][0]>.5),'reward':0.,'details':{}}}\n"
            (directory/'rubrics.py').write_text(rubric)
            manifest['sourceDigests']['rubrics.py']=hashlib.sha256(rubric.encode()).hexdigest()
            for case in manifest['tasks'][0]['evaluation']['cases']:case['expected']={'success':False}
        elif outcome is None:
            rubric = "def evaluate(task,facts,state):\n    return {'state':None,'result':{'success':None,'terminated':False,'reward':.25,'details':{}}}\n"
            (directory/'rubrics.py').write_text(rubric)
            manifest['sourceDigests']['rubrics.py'] = hashlib.sha256(rubric.encode()).hexdigest()
            for case in manifest['tasks'][0]['evaluation']['cases']:
                case['expected'] = {'success':None,'reward':.25}
        if array_details:
            from tests.test_generated_rubrics import SOURCE
            rubric=SOURCE.replace("'details': {'count': count}", "'details': {'count': count, 'points': (facts['initialSnapshot']['truth'] if facts['phase']=='reset' else facts['truthAfter'])['streams']['points']}")
            (directory/'rubrics.py').write_text(rubric)
            manifest['sourceDigests']['rubrics.py']=hashlib.sha256(rubric.encode()).hexdigest()
        expert = 'def run(env,task):\n    env.step('+repr(HOLD)+')\n'
        if outcome is True:expert += '    env.step('+repr(HOLD)+')\n'
        if outcome == 'truncated':
            manifest['tasks'][0]['execution']['physicalSeconds']=.02
            manifest['tasks'][0]['evaluation']['cases'][0]['expected']={'success':False,'reward':1.}
            case_source='def run(env,task):\n    env.step('+repr(HOLD)+')\n'
            (directory/'case_two.py').write_text(case_source)
            manifest['sourceDigests']['case_two.py']=hashlib.sha256(case_source.encode()).hexdigest()
        (directory/'expert.py').write_text(expert)
        manifest['sourceDigests']['expert.py'] = hashlib.sha256(expert.encode()).hexdigest()
        cases = []
        for case in manifest['tasks'][0]['evaluation']['cases']:
            payload = fixture.payload(operation='rollout',purpose='verification',root=str(root),jobId='fixture',
                controller={'kind':'policy','policyRef':case['sourceRef'],'caseId':case['id'],'digest':manifest['sourceDigests'][case['sourceRef']]})
            result = worker.rollout(payload,root);worker.finalize(payload,result);cases.append(result['episode'])
        payload = fixture.payload(operation='rollout',root=str(root),jobId='fixture')
        original = worker.rollout(payload,root);worker.finalize(payload,original)
        real = worker.run_controller
        def control(*args,**kwargs):
            if kwargs.get('kind') == 'isolation':return {'isolationChecks':{**{name:True for name,_,_ in kwargs['probes']},'networkBlocked':True}}
            return real(*args,**kwargs)
        self.enterContext(patch.object(worker,'run_controller',side_effect=control))
        selection = {'episodeId':original['episode'],'taskId':'events','use':'execution_data'}
        request = fixture.payload(operation='consume',root=str(root),episodes=[selection],validationEpisodes=cases,requestId='consumer')
        return fixture,project,directory,manifest,request,original

    def test_complete_failure_and_continuous_data_do_not_require_a_second_success(self):
        for outcome in (False,None):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as temporary:
                fixture,project,directory,manifest,payload,original = self.fixture(Path(temporary),outcome=outcome)
                with patch.object(worker,'replay',create=True,side_effect=AssertionError('full replay is not a delivery gate')):
                    result = worker.consume_internal(payload,Path(temporary))
                self.assertEqual(result['schema'],'sceneagent-product-consumption-v1')
                self.assertTrue(result['passed']);self.assertNotIn('replay',result['demonstrations'])
                batch = result['demonstrations']['batches'][0]
                self.assertIs(batch['result']['success'],outcome);self.assertFalse(batch['demonstrationEligible'])
                self.assertEqual(batch['use'],'execution_data');self.assertNotIn('firstSample',batch)
                self.assertTrue((Path(temporary)/batch['recordRef']).is_file())
                self.assertTrue((Path(temporary)/batch['transitionsRef']).is_file())
                interface = result['evaluation']['interfaces'][0]
                self.assertTrue(interface['passed']);self.assertEqual(interface['controlCycles'],1)
                self.assertEqual(interface['physicalSteps'],10);self.assertEqual(interface['coverage']['step'],'checked')
                self.assertTrue(worker.required_files(payload,result))

    def test_large_recorded_arrays_survive_actual_consumer_tool_return_and_trace_restore(self):
        from examples import read_generation
        from runtime.records import load_batch, read_array
        import numpy as np
        with tempfile.TemporaryDirectory() as temporary:
            _,project,_,_,payload,original=self.fixture(Path(temporary),visible_array=True)
            result=worker.consume_internal(payload,Path(temporary))
            self.assertTrue(result['passed'])
            batch=result['demonstrations']['batches'][0]
            episode=project/'episodes'/original['episode']
            recorded=load_batch(episode)
            reference=recorded['observations'][0]['streams']['points']
            self.assertEqual(set(reference),{'ref','sha256','dtype','shape'})
            self.assertEqual(reference['shape'],[168,3]);self.assertTrue(reference['ref'].startswith('arrays/'))
            np.testing.assert_array_equal(read_array(episode,reference),np.arange(168*3,dtype=np.float32).reshape(168,3))
            self.assertEqual(Path(temporary)/batch['recordRef'],episode/'records.json')
            self.assertEqual(Path(temporary)/batch['transitionsRef'],episode/'transitions.jsonl')
            self.assertEqual(recorded['actions'][0],HOLD)
            self.assertEqual(recorded['nextObservations'][0]['streams']['points'],reference)
            events=[{'eventId':'p1:1','type':'tool_call','generationId':'g1','requirementVersion':1,
                     'actor':'codex-mcp','name':'project_consume','requestId':'consumer','args':{'episodes':payload['episodes']}},
                    {'eventId':'p1:2','type':'tool_result','generationId':'g1','requirementVersion':1,
                     'name':'project_consume','callEventId':'p1:1','result':result}]
            trace=''.join(json.dumps(event)+'\n' for event in events).encode()
            trace_ref='consumer-reports/traces/'+hashlib.sha256(trace).hexdigest()+'.jsonl'
            target=project/trace_ref;target.parent.mkdir(parents=True);target.write_bytes(trace)
            report=read_generation.consume(project,'g1',trace_ref=trace_ref)
            self.assertTrue(report['passed'],report['issues'])
            restored=list(read_generation.iter_samples(project,report))
            self.assertEqual(restored[0]['returned'],result)
            self.assertEqual(restored[0]['returned']['demonstrations']['batches'][0]['recordRef'],batch['recordRef'])

    def test_generated_result_array_owners_survive_consume_and_case_tool_trace_restore(self):
        from examples import read_generation
        from runtime.records import read_array
        import numpy as np
        with tempfile.TemporaryDirectory() as temporary:
            _,project,_,_,payload,_=self.fixture(Path(temporary),array_details=True)
            result=worker.consume_internal(payload,Path(temporary))
            checked=worker.check({**payload,'episodeIds':payload['validationEpisodes']},Path(temporary))
            for name,returned in [('project_consume',result),('task_check',checked)]:
                with self.subTest(tool=name):
                    events=[{'eventId':'p1:1','type':'tool_call','generationId':'g1','requirementVersion':1,
                             'actor':'codex-mcp','name':name,'requestId':name,'args':{}},
                            {'eventId':'p1:2','type':'tool_result','generationId':'g1','requirementVersion':1,
                             'name':name,'callEventId':'p1:1','result':returned}]
                    trace=''.join(json.dumps(event)+'\n' for event in events).encode()
                    trace_ref='consumer-reports/traces/'+hashlib.sha256(trace).hexdigest()+'.jsonl'
                    target=project/trace_ref;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(trace)
                    report=read_generation.consume(project,'g1',trace_ref=trace_ref)
                    self.assertTrue(report['passed'],report['issues'])
                    self.assertEqual(list(read_generation.iter_samples(project,report))[0]['returned'],returned)
            rows=[*result['demonstrations']['batches'],*result['evaluation']['tasks'][0]['cases']]
            for row in rows:
                self.assertEqual((row['projectId'],row['revision']),('p1','rev1'))
                episode=project/'episodes'/row['episodeId']
                self.assertEqual(Path(temporary)/row['artifacts']['manifest'],episode/'manifest.json')
                reference=row['result']['details']['points']
                np.testing.assert_array_equal(read_array(episode,reference),np.arange(168*3,dtype=np.float32).reshape(168,3))
            self.assertTrue(worker.required_files(payload,result))
            for kind in ('batch','case'):
                with self.subTest(forgedOwner=kind):
                    changed=deepcopy(result)
                    row=(changed['demonstrations']['batches'][0] if kind=='batch'
                         else changed['evaluation']['tasks'][0]['cases'][0])
                    row['artifacts']['manifest']='projects/p1/episodes/wrong/manifest.json'
                    (Path(temporary)/changed['artifactRef']).write_text(json.dumps({k:v for k,v in changed.items()if k!='artifactRef'}))
                    with self.assertRaises(ValueError):worker.required_files(payload,changed)

    def test_false_or_none_execution_data_cannot_be_declared_a_successful_demonstration(self):
        for outcome in (False,None):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as temporary:
                _,_,_,_,payload,_ = self.fixture(Path(temporary),outcome=outcome)
                payload['episodes'][0]['use']='successful_demonstration'
                with self.assertRaisesRegex(ValueError,'success|successful'):
                    worker.consume_internal(payload,Path(temporary))

    def test_old_strings_and_forged_interface_passed_or_source_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _,_,_,_,payload,original = self.fixture(Path(temporary))
            legacy=deepcopy(payload);legacy['episodes']=[original['episode']]
            with self.assertRaises(ValueError):worker.consume_internal(legacy,Path(temporary))
            result=worker.consume_internal(payload,Path(temporary))
            report=Path(temporary)/result['artifactRef']
            for name,value in [('physicalSteps',0),('sourceFactsDigest','0'*64),('sourceEpisodeId','wrong'),('passed',False)]:
                with self.subTest(field=name):
                    changed=deepcopy(result);changed['evaluation']['interfaces'][0][name]=value
                    report.write_text(json.dumps({k:v for k,v in changed.items()if k!='artifactRef'}))
                    with self.assertRaises(ValueError):worker.required_files(payload,changed)

    def test_successful_original_is_not_invalidated_by_a_false_probe_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            _,_,_,_,payload,original=self.fixture(Path(temporary),outcome=True)
            payload['episodes'][0]['use']='successful_demonstration'
            result=worker.consume_internal(payload,Path(temporary));self.assertTrue(result['passed'])
            batch=result['demonstrations']['batches'][0];self.assertTrue(batch['demonstrationEligible']);self.assertTrue(batch['result']['success'])
            probe=Path(temporary)/result['evaluation']['interfaces'][0]['episodeRef']
            manifest=json.loads(probe.read_text());self.assertFalse(manifest['result']['success']);self.assertFalse(manifest['demonstrationEligible'])
            self.assertTrue(worker.required_files(payload,result))

    def test_initial_terminal_only_records_zero_steps_and_real_rejected_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture,_,_,_,payload,_=self.fixture(Path(temporary),outcome='terminal')
            fixture._system_fixture_streams['q']=[1.]+HOLD['arm'][1:7]+[.04,.04]
            result=worker.consume_internal(payload,Path(temporary));row=result['evaluation']['interfaces'][0]
            self.assertEqual((row['controlCycles'],row['physicalSteps']),(0,0));self.assertEqual(row['coverage']['step'],'not_exercised')
            self.assertEqual(row['coverage']['endedStepRejection'],'checked');self.assertTrue(worker.required_files(payload,result))
            probe=Path(temporary)/row['episodeRef'];manifest=json.loads(probe.read_text())
            self.assertEqual(manifest['control']['actionAttempts'],1);self.assertEqual(manifest['control']['rejectedActionCalls'],1)
            self.assertEqual(manifest['control']['actionCalls'],0)
            changed=deepcopy(result);changed['evaluation']['interfaces'][0]['coverage']['step']='checked'
            (Path(temporary)/changed['artifactRef']).write_text(json.dumps({k:v for k,v in changed.items()if k!='artifactRef'}))
            with self.assertRaises(ValueError):worker.required_files(payload,changed)

    def test_truncated_window_and_different_fresh_state_remain_honest_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture,_,_,_,payload,_=self.fixture(Path(temporary),outcome='truncated')
            fixture._system_fixture_streams['q']=[.7]+HOLD['arm'][1:7]+[.04,.04]
            result=worker.consume_internal(payload,Path(temporary));self.assertTrue(result['passed'])
            self.assertFalse(result['demonstrations']['batches'][0]['demonstrationEligible'])
            self.assertTrue(worker.required_files(payload,result))

    def test_probe_commands_preserve_explicit_values_and_require_nonpersistent_ports(self):
        from tests.test_records import BASE_IO
        io=deepcopy(BASE_IO);io['ports']['arm'].update(initialized=True,persistence='persistent')
        effort=deepcopy(io['ports']['arm']);effort.update(shape=[1],units=['N*m'],limits=[[-10,10]],persistence='per_physics_step')
        effort['source'].update(primitive='joint_effort',joints=['effort_joint']);io['ports']['effort']=effort
        action={'effort':[.75]};selected=worker._interface_source(action,io)
        class Proxy:
            def __init__(self):self.sent=[]
            def open(self):return {'io':io}
            def observe(self):return {'terminated':False}
            def step(self,values,seconds):self.sent.append((values,seconds))
        env=Proxy();namespace={};exec(selected['sourceFiles']['interface_probe.py'],namespace);namespace['run'](env,None)
        self.assertEqual(env.sent,[(action,.02)]);self.assertNotIn('arm',env.sent[0][0])
        with self.assertRaisesRegex(ValueError,'nonpersistent'):worker._interface_source({},io)
        io['ports']['arm']['initialized']=False
        with self.assertRaisesRegex(ValueError,'uninitialized'):worker._interface_source(action,io)

    def test_matching_case_result_cannot_hide_policy_environment_or_score_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            _,project,directory,manifest,payload,_=self.fixture(Path(temporary))
            original=worker.verify_episode
            for execution,score in [('policy_error','available'),('environment_error','available'),('completed','unavailable')]:
                with self.subTest(execution=execution,score=score):
                    def changed(*args,**kwargs):
                        report,files=original(*args,**kwargs)
                        return {**report,'executionStatus':execution,'scoreStatus':score},files
                    with patch.object(worker,'verify_episode',side_effect=changed):
                        rows=worker.validation_cases(Path(temporary),project,directory,manifest,worker.frozen(Path(temporary),{})[3],payload['validationEpisodes'])
                    self.assertFalse(any(row['passed'] for row in rows))

    def test_truncated_record_cannot_bypass_matching_isolated_controller_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            _,project,directory,manifest,_,original=self.fixture(Path(temporary),outcome='truncated')
            episode=project/'episodes'/original['episode'];path=episode/'manifest.json';saved=json.loads(path.read_text())
            world=worker.frozen(Path(temporary),{})[3]
            self.assertEqual(worker.verify_episode(Path(temporary),episode,directory,manifest,world)[0]['executionStatus'],'truncated')
            for key,value in [('isolated',False),('kind','candidate'),('policyDigest','0'*64),('openCalls',0),('resetCalls',0)]:
                with self.subTest(field=key):
                    forged=deepcopy(saved);forged['control'][key]=value;path.write_text(json.dumps(forged))
                    with self.assertRaisesRegex(ValueError,'isolated controller'):
                        worker.verify_episode(Path(temporary),episode,directory,manifest,world)
                    path.write_text(json.dumps(saved))
