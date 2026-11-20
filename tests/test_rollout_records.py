"""SYSTEM_VERIFICATION_ONLY physical-tail publication; no generated task or native execution."""
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runtime import worker
from runtime.records import Recorder, load_batch, atomic_json
from runtime.scene import Task, World
from tests.test_records import RubricFixture, COMMAND, BASE_IO, observation
from copy import deepcopy


class RolloutRecordTests(unittest.TestCase):
    def test_execution_budget_is_bound_to_the_call_in_every_new_receipt(self):
        from tests.test_frozen_evaluation import FrozenEvaluationTests
        for changed in ('all_wrong','manifest_wrong','report_wrong','missing'):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as temporary:
                fixture=FrozenEvaluationTests()
                try:
                    root=Path(temporary).resolve();project,frozen,build,world=fixture.context(root)
                    build['runtimeExecution']={'workerTimeoutParameter':'workerTimeoutSeconds'}
                    payload={**fixture.payload(),'root':str(root),'operation':'rollout','workerTimeoutSeconds':1800}
                    result=worker.rollout(payload,root)
                    self.assertTrue(worker.required_files(payload,result))
                    directory=project/'episodes'/result['episode']
                    mp=directory/'manifest.json';rp=directory/'report.json'
                    manifest=json.loads(mp.read_text());report=json.loads(rp.read_text())
                    targets=([manifest,report,result,*result['episodes']] if changed in ('all_wrong','missing')
                             else [manifest] if changed=='manifest_wrong' else [report])
                    for value in targets:
                        if changed=='missing':value.pop('workerTimeoutSeconds',None)
                        else:value['workerTimeoutSeconds']=17
                    result['score']=deepcopy(report);result['episodes'][0]['score']=deepcopy(report)
                    atomic_json(rp,report);manifest['artifactDigests']['report']=worker.digest(rp);atomic_json(mp,manifest)
                    with self.assertRaisesRegex(ValueError,'timeout|budget'):
                        worker.required_files(payload,result)
                finally:fixture.doCleanups()

    def test_legacy_missing_budget_fields_remain_readable_without_new_capability(self):
        from tests.test_frozen_evaluation import FrozenEvaluationTests
        fixture=FrozenEvaluationTests();self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();project,frozen,build,world=fixture.context(root)
            payload={**fixture.payload(),'root':str(root),'operation':'rollout'};result=worker.rollout(payload,root)
            directory=project/'episodes'/result['episode'];mp=directory/'manifest.json';rp=directory/'report.json'
            manifest=json.loads(mp.read_text());report=json.loads(rp.read_text())
            for value in [manifest,report,result,*result['episodes']]:value.pop('workerTimeoutSeconds',None)
            result['score']=deepcopy(report);result['episodes'][0]['score']=deepcopy(report)
            atomic_json(rp,report);manifest['artifactDigests']['report']=worker.digest(rp);atomic_json(mp,manifest)
            self.assertTrue(worker.required_files(payload,result))

    def test_record_witness_hashes_are_established_before_parsing(self):
        from runtime import records
        from tests.test_frozen_evaluation import FrozenEvaluationTests
        fixture = FrozenEvaluationTests();self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project,frozen,build,world = fixture.context(root)
            result = worker.rollout(fixture.payload(),root)
            directory = project/'episodes'/result['episode']
            original = records.read_transitions
            def read(*args,**kwargs):
                prior = [Path(call.args[0]) for call in digests.call_args_list]
                self.assertEqual(prior.count(directory/'records.json'),1)
                self.assertEqual(prior.count(directory/'transitions.jsonl'),1)
                return original(*args,**kwargs)
            with patch.object(worker,'digest',wraps=worker.digest) as digests, \
                 patch.object(records,'read_transitions',side_effect=read):
                worker.verify_episode(root,directory,frozen,build,world)

    def test_final_scan_hashes_each_input_once_without_reopening_arrays(self):
        import numpy as np
        from runtime import records
        from tests.test_frozen_evaluation import FrozenEvaluationTests
        from tests.test_native_records import IO
        fixture = FrozenEvaluationTests();self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project,frozen,build,world = fixture.context(root)
            build['io']['streams']['nodes'] = deepcopy(IO['streams']['nodes'])
            build['compiled']['io'] = deepcopy(build['io'])
            fixture._system_fixture_streams['nodes'] = np.arange(210,dtype=np.float32).reshape(70,3)
            result = worker.rollout(fixture.payload(),root)
            directory = project/'episodes'/result['episode']
            index = json.loads((directory/'records.json').read_text())
            reference = index['initialSnapshot']['truth']['streams']['nodes']
            calls,checkpoint = [],{}
            original = records.project_annotations
            def last(*args,**kwargs):
                value = original(*args,**kwargs);calls.append(True)
                if len(calls)==2:
                    checkpoint.update(digests=len(digests.call_args_list),arrays=arrays.call_count)
                return value
            with patch.object(worker,'digest',wraps=worker.digest) as digests, \
                 patch.object(np,'load',wraps=np.load) as arrays, \
                 patch.object(records,'project_annotations',side_effect=last):
                worker.verify_episode(root,directory,frozen,build,world)
            sealed = [Path(call.args[0]) for call in digests.call_args_list[checkpoint['digests']:]]
            self.assertCountEqual(sealed,[directory/'records.json',directory/'transitions.jsonl',directory/reference['ref']])
            self.assertEqual(arrays.call_count,checkpoint['arrays'])

    def test_late_record_and_array_changes_are_rejected_after_the_last_consumer(self):
        import numpy as np
        from runtime import records
        from tests.test_frozen_evaluation import FrozenEvaluationTests
        from tests.test_native_records import IO
        for artifact in ('records.json','transitions.jsonl','array'):
            for mutation in ('content','replacement','symlink'):
                with tempfile.TemporaryDirectory() as temporary,self.subTest(artifact=artifact,mutation=mutation):
                    fixture = FrozenEvaluationTests()
                    try:
                        root = Path(temporary).resolve()
                        project,frozen,build,world = fixture.context(root)
                        build['io']['streams']['nodes'] = deepcopy(IO['streams']['nodes'])
                        build['compiled']['io'] = deepcopy(build['io'])
                        fixture._system_fixture_streams['nodes'] = np.arange(210,dtype=np.float32).reshape(70,3)
                        result = worker.rollout(fixture.payload(),root)
                        directory = project/'episodes'/result['episode']
                        index = json.loads((directory/'records.json').read_text())
                        target = directory/(index['initialSnapshot']['truth']['streams']['nodes']['ref'] if artifact=='array' else artifact)
                        calls = []
                        original = records.project_annotations
                        def late(*args,**kwargs):
                            value = original(*args,**kwargs);calls.append(True)
                            if len(calls)==2:
                                raw = target.read_bytes()
                                if mutation=='content':target.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
                                elif mutation=='replacement':
                                    replacement = target.with_suffix('.replacement')
                                    replacement.write_bytes(raw);replacement.replace(target)
                                else:
                                    replacement = root/'late-reference.bin';replacement.write_bytes(raw)
                                    target.unlink();target.symlink_to(replacement)
                            return value
                        with patch.object(records,'project_annotations',side_effect=late),self.assertRaises(ValueError):
                            worker.verify_episode(root,directory,frozen,build,world)
                        self.assertEqual(len(calls),2)
                    finally:
                        fixture.doCleanups()

    def test_each_verification_reads_records_once_without_reusing_a_later_pass(self):
        from runtime import records
        from tests.test_frozen_evaluation import FrozenEvaluationTests
        fixture = FrozenEvaluationTests()
        self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project,frozen,build,world = fixture.context(root)
            result = worker.rollout(fixture.payload(),root)
            directory = project/'episodes'/result['episode']
            with patch.object(records,'read_transitions',wraps=records.read_transitions) as read:
                first,required = worker.verify_episode(root,directory,frozen,build,world)
                self.assertEqual(first,result['score'])
                self.assertIn(directory/'transitions.jsonl',required)
                self.assertEqual(read.call_count,1)
                second,again = worker.verify_episode(root,directory,frozen,build,world)
                self.assertEqual((second,again),(first,required))
                self.assertEqual(read.call_count,2)
                path = directory/'transitions.jsonl'
                original = path.read_bytes();changed = original.replace(b'"q": [0',b'"q": [1',1)
                self.assertNotEqual(original,changed);self.assertEqual(len(original),len(changed))
                path.write_bytes(changed)
                with self.assertRaises(ValueError):
                    worker.verify_episode(root,directory,frozen,build,world)
                self.assertEqual(read.call_count,2)  # The fresh initial SHA rejects changed bytes before parsing.

    def fixture(self, root):
        project, frozen = root/'projects/p1', root/'projects/p1/revisions/r1'
        frozen.mkdir(parents=True)
        files = {'scene.py':'', 'tasks.py':'', 'expert.py':'def run(env, task): pass\n',
                 'rubrics.py':'def evaluate(task, facts, state): raise NotImplementedError\n'}
        hashes = {}
        for name,text in files.items():
            (frozen/name).write_text(text); hashes[name] = hashlib.sha256(text.encode()).hexdigest()
        world = World(native=True); world.bind('item','/World/item')
        task = Task('fixture', bindings={'item':'item'}, rubric={'sourceRef':'rubrics.py','helperRefs':[]})
        manifest = {'projectId':'p1', 'revision':'r1', 'runtime':'system-fixture', 'seed':0, 'tasks':[task.bind(world)],
            'artifacts':{'manifest':'projects/p1/revisions/r1/manifest.json'}, 'sourceDigests':hashes,
            'io':deepcopy(BASE_IO), 'compiled':{'native':True,'io':deepcopy(BASE_IO)}}
        class Session(RubricFixture):
            def __init__(self, task, selected, **kwargs):
                super().__init__(success=True)
                self.descriptor = {key:selected[key] for key in ('sourceRef','sourceDigest','helperDigests')}
            def close(self): pass
        def load(directory, build, world, output, task, **kwargs):
            recorder = Recorder(output, io=build['io'],
                recording=task.recording, task=asdict(task))
            recorder.begin({'observation':observation(0), 'truth':deepcopy(observation(0))}, kwargs['rubric_session'])
            for tick in range(15):
                recorder.commit(observation(tick*.02),COMMAND,COMMAND,observation((tick+1)*.02),10,
                    truth_before=deepcopy(observation(tick*.02)),truth_after=deepcopy(observation((tick+1)*.02)))
            (output/'video.mp4').write_bytes(b'SYSTEM_VERIFICATION_ONLY diagnostic video')
            tail = observation(.32)
            failure = {'countStatus':'known', 'totalExecutedPhysicalSteps':160, 'actualTail':tail,
                'actualClock':{key:tail[key] for key in ('physicsStep','time','simulationTime','timeOrigin','stageId')}}
            atomic_json(output/'native-step-failure.json', failure)
            return SimpleNamespace(recorder=recorder, io=build['io'], compiled=build['compiled'], steps=160,
                active_targets=deepcopy(COMMAND),
                _failed=failure,
                time=.32, frames=[],
                close=recorder.close)
        error = 'live USD robot link differs from actual tensor: /World/franka/panda_link1'
        with patch.object(worker,'frozen',return_value=(project,frozen,manifest,world)), \
             patch.object(worker,'RubricSession',Session), patch.object(worker,'load_environment',side_effect=load), \
             patch.object(worker,'run_controller',side_effect=RuntimeError(error)), patch.object(worker,'progress'):
            result = worker.rollout({'projectId':'p1'},root)
        return project/'episodes'/result['episode'], frozen, manifest, world, Session, result, error

    def test_ordinary_episode_records_its_actual_initial_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, _, _, _, _, _, _ = self.fixture(Path(temporary).resolve())
            manifest = json.loads((directory/'manifest.json').read_text())
            self.assertEqual(manifest.get('nativeInitialTargets'), COMMAND)

    def test_failed_physical_tail_preserves_prefix_and_never_publishes_its_score_as_final(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory,_,_,_,_,result,error = self.fixture(Path(temporary).resolve())
            manifest = json.loads((directory/'manifest.json').read_text())
            batch = load_batch(directory)
            self.assertEqual((manifest['controlCycles'],manifest['physicalSteps'],manifest['totalExecutedPhysicalSteps']), (15,150,160))
            self.assertEqual(manifest['uncommittedPhysicalSteps'],10)
            self.assertEqual(batch['physicalSteps'],150)
            self.assertTrue(batch['lastResult']['success'])
            self.assertFalse(manifest['demonstrationEligible'])
            self.assertFalse(manifest['candidateDemonstrationEligible'])
            self.assertIsNone(result['score']['success'])
            self.assertIsNone(result['score']['result'])
            self.assertEqual(result['score']['scoreStatus'],'unavailable')
            self.assertIn(error,result['score']['error']['message'])
            self.assertTrue((directory/'video.mp4').exists())
            self.assertEqual(json.loads((directory/'native-step-failure.json').read_text())['actualTail']['physicsStep'],160)

    def test_parent_rejects_a_fake_normal_success_with_uncommitted_actual_steps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory,frozen,build,world,Session,_,_ = self.fixture(root)
            manifest = json.loads((directory/'manifest.json').read_text())
            report = json.loads((directory/'report.json').read_text())
            committed = load_batch(directory)['lastResult']
            control = {'isolated':True,'kind':'expert','policyDigest':manifest['controllerDigest'],'openCalls':1,'resetCalls':1}
            report.update(executionStatus='completed',result=committed,scoreStatus='available',error=None,control=control,**committed)
            manifest.update(executionStatus='completed',result=committed,control=control,candidateDemonstrationEligible=True)
            atomic_json(directory/'manifest.json',manifest); atomic_json(directory/'report.json',report)
            with patch.object(worker,'RubricSession',Session), self.assertRaisesRegex(ValueError,'incomplete actual execution'):
                worker.verify_episode(root,directory,frozen,build,world)


if __name__ == '__main__': unittest.main()
