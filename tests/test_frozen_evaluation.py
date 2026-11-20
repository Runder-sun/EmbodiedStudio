"""SYSTEM_VERIFICATION_ONLY: fake physics/isolation, real grading subprocess and records.

Empty recording is intentional. Media verification still runs over the unique
empty frame table; no pixels or simulator qualification are claimed.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtime import worker
from runtime.records import Recorder, read_transitions, project_annotations
from runtime.scene import Task, World
from tests.test_generated_rubrics import SOURCE

from tests.test_records import BASE_IO
HOLD = {'arm':[0, -.4, 0, -2, 0, 1.6, .7854, .08]}
TWO = 'def run(env, task):\n    env.step('+repr(HOLD)+')\n    env.step('+repr(HOLD)+')\n'
ZERO = 'def run(env, task):\n    pass\n'


class FrozenEvaluationTests(unittest.TestCase):
    def fixture(self, root):
        self._system_fixture_streams = {}
        cpu_streams = self._system_fixture_streams
        project = root/'projects/p1'; directory = project/'revisions/rev1'; directory.mkdir(parents=True)
        world = World(native=True); world.bind('cube','/World/cube')
        task = Task('events', bindings={'object': 'cube'}, parameters={'count': 2},
            rubric={'sourceRef': 'rubrics.py', 'helperRefs': []}, evaluation={'cases': [
                {'id': 'two', 'sourceRef': 'case_two.py', 'helperRefs': [], 'expected': {'success': True, 'reward': 2.}, 'requirementRefs': ['fixture']},
                {'id': 'zero', 'sourceRef': 'case_zero.py', 'helperRefs': [], 'expected': {'success': False, 'reward': 0.}, 'requirementRefs': ['fixture']}]})
        files = {'scene.py': '', 'tasks.py': '', 'expert.py': TWO, 'rubrics.py': SOURCE, 'case_two.py': TWO, 'case_zero.py': ZERO}
        for name, source in files.items():
            (directory/name).write_text(source)
        manifest = {'schema': 'sceneagent-build-v6', 'engine': 'isaacsim', 'physics': 'physx',
            'projectId': 'p1', 'revision': 'rev1', 'seed': 0, 'runtime': 'fixture', 'tasks': [task.bind(world)],
            'assets': {}, 'sourceDigests': {name: hashlib.sha256(source.encode()).hexdigest() for name, source in files.items()},
            'compiled': {'native':True,'io':deepcopy(BASE_IO)}, 'camera': {'fixture': True}, 'io':deepcopy(BASE_IO),
            'artifacts': {'manifest': 'projects/p1/revisions/rev1/manifest.json'}}
        for name in manifest['io']['ports']:
            manifest['io']['ports'][name]['initialized'] = True
        manifest['compiled']['io'] = deepcopy(manifest['io'])
        (directory/'manifest.json').write_text(json.dumps(manifest)); (directory/'scene.json').write_text(json.dumps(world.snapshot()))

        class Environment:
            def __init__(self, output, session, record=True, definition=task):
                task=definition
                self.directory, self.world, self.task = output, world, task
                self.io, self.camera_spec, self.initial_state = manifest['io'], manifest['camera'], None
                self.compiled = manifest['compiled']
                self.active_targets = {name:list(HOLD[name]) for name in self.io['ports']}
                self.camera_failures = {camera['cameraId']:'CPU sensor unavailable before its first actual frame'
                                        for camera in task.recording['cameras']}
                self.steps, self.time, self.events, self.logical_state, self.contacts, self.frames = 0, 0., [], {}, set(), []
                self.physical_seconds = task.execution['physicalSeconds']
                self.recorder = None
                output.mkdir(parents=True, exist_ok=True)
                self.initial_state = self.export_state()
                if record:
                    self.recorder = Recorder(output, io=manifest['io'],
                        recording=task.recording, task=asdict(task))
                    self.recorder.begin({'observation': self.observe(), 'truth': self.observe(truth=True)}, session)

            @property
            def last_result(self):
                return self.recorder.last_result if self.recorder else None

            @property
            def truncated(self):
                return self.recorder.index['truncated'] if self.recorder else False

            @property
            def episode_done(self):
                return self.recorder.episode_done if self.recorder else False

            def export_state(self):
                return self.observe(truth=True)

            def observe(self, *args, truth=False):
                if not truth and self.recorder is not None and self.recorder.committed_observation is not None:
                    return self.recorder.committed_observation
                return self._observe_raw(*args, truth=truth)

            def _observe_raw(self, *args, truth=False):
                row = {'time': self.time, 'simulationTime': 1+self.time, 'timeOrigin': 1., 'physicsStep': self.steps,
                       'stageId':1,'streams':{'q':HOLD['arm'][:7]+[.04,.04],'dq':[0.]*9},'streamStatus':{},'cameraFrames':{}}
                for name,value in cpu_streams.items():
                    # Explicit CPU input only; this fake Environment is never SDK evidence.
                    if truth or self.io['streams'][name]['visibleToPolicy']:
                        import numpy as np
                        row['streams'][name]=np.asarray(value,dtype=self.io['streams'][name]['dtype'])
                if truth:
                    row['cameraFrames'] = {name:{'cameraId':name,'status':'unavailable','reason':reason}
                                           for name,reason in self.camera_failures.items()}
                return row

            def reset(self, seed):
                if seed != 0 or self.steps:
                    raise ValueError('fresh episode required')
                return self.observe()

            def step(self, action, seconds=.02):
                if self.episode_done:
                    raise ValueError('ended or failed native episode requires a fresh host process')
                for _ in range(round(seconds*50)):
                    if self.episode_done:
                        break
                    before, truth = self.observe(), self.observe(truth=True)
                    self.steps += 10; self.time = self.steps/500
                    self.recorder.commit(before, action, action, self._observe_raw(), 10,
                        truth_before=truth, truth_after=self.observe(truth=True), truncated=self.time >= self.physical_seconds-1e-9)
                return self.observe()

            def close(self, reason='completed'):
                if self.recorder:
                    self.recorder.close(reason)

        return project, directory, manifest, world, Environment

    def context(self, root):
        project, directory, manifest, world, environment = self.fixture(root)
        self.enterContext(patch.object(worker, 'frozen', return_value=(project, directory, manifest, world)))
        self.enterContext(patch.object(worker, 'load_environment', side_effect=lambda directory, manifest, world, output, task,
            record=True, **kwargs: environment(output, kwargs.get('rubric_session'), record, task)))
        self.enterContext(patch.object(worker, 'controller_command', side_effect=lambda entry='--controller', **kwargs:
            [sys.executable, '-c', 'from runtime.worker import '+('rubric_entry' if entry == '--rubric' else 'controller_entry')+'; '+('rubric_entry' if entry == '--rubric' else 'controller_entry')+'()']))
        self.enterContext(patch.object(worker, 'validate_publication'))
        return project, directory, manifest, world

    def payload(self, **fields):
        return {'projectId': 'p1', 'revision': 'rev1', **fields}

    def candidate(self, source):
        return {'sourceRef': 'candidate.py', 'helperRefs': [], 'sourceFiles': {'candidate.py': source},
                'sourceDigests': {'candidate.py': hashlib.sha256(source.encode()).hexdigest()}}

    def test_long_episode_budgets_preserve_controller_and_rubric_protection(self):
        self.assertEqual(worker.episode_budgets({}), {
            'physicalSeconds': 30, 'controllerSeconds': 120, 'rubricSeconds': 30})
        for seconds in (301, 1800, 86400.):
            task = Task('long', execution={'physicalSeconds': seconds})
            with self.subTest(seconds=seconds):
                self.assertEqual(worker.episode_budgets({}, task), {
                    'physicalSeconds': seconds, 'controllerSeconds': 120, 'rubricSeconds': 30})
                self.assertEqual(worker.episode_budgets({'maxSeconds': 7, 'rubricSeconds': 3}, task), {
                    'physicalSeconds': seconds, 'controllerSeconds': 7, 'rubricSeconds': 3})
                for payload in ({'rubricSeconds': 0}, {'maxSeconds': None}):
                    with self.assertRaises(ValueError):worker.episode_budgets(payload, task)
        for seconds in (True, False, None, '301', 0, -1, float('nan'), float('inf'), -float('inf')):
            with self.subTest(invalid=seconds), self.assertRaises(ValueError):
                worker.episode_budgets({}, Task('invalid', execution={'physicalSeconds': seconds}))

    def test_explicit_controller_compute_budget_can_exceed_default_without_changing_physics(self):
        for seconds in (50, 90):
            task = Task('compute_budget', execution={'physicalSeconds': seconds})
            self.assertEqual(worker.episode_budgets({'maxSeconds': 600}, task), {
                'physicalSeconds': seconds, 'controllerSeconds': 600, 'rubricSeconds': 30})
        for value in (True, False, None, '600', 0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worker.episode_budgets({'maxSeconds': value})

    def test_explicit_rubric_budget_can_exceed_default_without_changing_physics(self):
        task = Task('grading_budget', execution={'physicalSeconds': 90})
        self.assertEqual(worker.episode_budgets({'rubricSeconds': 180}, task), {
            'physicalSeconds': 90, 'controllerSeconds': 120, 'rubricSeconds': 180})
        for value in (True, False, None, '180', 0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worker.episode_budgets({'rubricSeconds': value}, task)

    def test_rubric_replay_keeps_the_recorded_compute_budget(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); project, directory, manifest, _ = self.context(root)
            result = worker.rollout(self.payload(), root)
            episode = project/'episodes'/result['episode']
            receipt_path = episode/'manifest.json'
            receipt = json.loads(receipt_path.read_text())
            receipt['limits']['rubricSeconds'] = 60  # Isolated future-receipt fixture, not production data.
            receipt_path.write_text(json.dumps(receipt))
            task = Task(**manifest['tasks'][0])
            selected = worker.rubric_sources(task, worker.frozen_sources(directory, manifest), manifest['sourceDigests'])
            original = worker.RubricSession
            def replay_session(*args, **kwargs):
                session = original(*args, **kwargs)
                session.remaining -= 31  # Simulate already spent replay time without a slow test.
                return session
            with patch.object(worker, 'RubricSession', side_effect=replay_session):
                rebuilt = worker.recompute_rubric(task, selected, episode, io=manifest['io'])
            self.assertEqual(rebuilt['result'], result['score']['result'])
            self.assertEqual(rebuilt['scoreStatus'], 'available')

    def test_long_frozen_task_runs_through_existing_rollout_and_record_verification(self):
        for seconds in (301, 1800, 86400.):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); project, _, manifest, world = self.context(root)
                task = Task(**manifest['tasks'][0]); task.execution = {'physicalSeconds': seconds}
                manifest['tasks'] = [task.bind(world)]
                payload = self.payload(operation='rollout', root=str(root))
                result = worker.rollout(payload, root)
                self.assertTrue(worker.required_files(payload, result))
                episode = project/'episodes'/result['episode']
                receipt = json.loads((episode/'manifest.json').read_text())
                rows, index, tail = read_transitions(episode)
                self.assertEqual(receipt['limits'], {
                    'physicalSeconds': seconds, 'controllerSeconds': 120, 'rubricSeconds': 30})
                self.assertEqual(receipt['workerTimeoutSeconds'], 900)
                self.assertEqual((len(rows), index['physicalSteps'], tail), (2, 20, False))
                self.assertEqual(result['score']['executionStatus'], 'completed')
                self.assertFalse(index['truncated'])

    def test_build_parent_binds_each_tasks_distinct_physical_duration(self):
        """Real frozen CPU inputs; native collision/runtime verification isolated here."""
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();project,directory,manifest,world,_=self.fixture(root)
            first=Task(**manifest['tasks'][0]);first.execution={'physicalSeconds':60}
            second=deepcopy(first);second.id='second';second.execution={'physicalSeconds':120}
            manifest['tasks']=[first.bind(world),second.bind(world)]
            manifest['episodeLimits']={'tasks':{first.id:first.execution,second.id:second.execution},'reset':'frozen_seed_only'}
            for filename,data in (('tasks.json',json.dumps(manifest['tasks'])),('scene-source.usda','SYSTEM_VERIFICATION_ONLY native SDK isolated'),
                                  ('preview.glb','SYSTEM_VERIFICATION_ONLY native SDK isolated'),('native-stage-audit.json','SYSTEM_VERIFICATION_ONLY native SDK isolated')):
                (directory/filename).write_text(data)
            manifest['frozenArtifactDigests']={filename:worker.digest(directory/filename)
                for filename in ('scene.json','tasks.json','scene-source.usda','preview.glb','native-stage-audit.json')}
            manifest.setdefault('artifacts', {})['preview'] = str((directory/'preview.glb').relative_to(root))
            sources={filename:(directory/filename).read_text() for filename in manifest['sourceDigests']}
            payload=self.payload(operation='build',root=str(root),sourceFiles=sources)
            with patch.object(worker,'verify_inputs',return_value=set()),patch.object(worker,'runtime_key',return_value='fixture'):
                self.assertTrue(worker.required_files(payload,manifest))
                for mutation in ('old_fixed_default','missing_task','wrong_duration','swapped_durations'):
                    with self.subTest(mutation=mutation):
                        changed=deepcopy(manifest)
                        if mutation == 'old_fixed_default':changed['episodeLimits']={'physicalSeconds':30,'reset':'frozen_seed_only'}
                        elif mutation == 'missing_task':changed['episodeLimits']['tasks'].pop('second')
                        elif mutation == 'wrong_duration':changed['episodeLimits']['tasks']['events']={'physicalSeconds':30}
                        else:changed['episodeLimits']['tasks']={'events':{'physicalSeconds':120},'second':{'physicalSeconds':60}}
                        with self.assertRaisesRegex(ValueError,'episode limits.*frozen task'):
                            worker.required_files(payload,changed)

    def case_episodes(self, root, *, revision='rev1'):
        _, _, manifest, _ = worker.frozen(root, {'revision': revision})
        ids = []
        for name in ('two', 'zero'):
            ref = 'case_'+name+'.py'
            payload = self.payload(operation='rollout', root=str(root), revision=revision, purpose='verification', jobId='fixture',
                controller={'type':'policy', 'policyRef':ref, 'digest':manifest['sourceDigests'][ref]})
            run = worker.rollout(payload, root)
            worker.finalize(payload, run)
            ids.append(run['episode'])
        return ids

    def isolated_consumer(self):
        original = worker.run_controller
        def controller(*args, **kwargs):
            if kwargs.get('kind') == 'isolation':
                return {'isolationChecks':{**{name:True for name,_,_ in kwargs['probes']}, 'networkBlocked':True}}
            return original(*args, **kwargs)
        return patch.object(worker, 'run_controller', side_effect=controller)

    def test_environment_only_consumer_uses_actual_cases_without_expert(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); _, directory, manifest, _ = self.context(root)
            manifest['sourceDigests'].pop('expert.py'); (directory/'expert.py').unlink()
            ids = self.case_episodes(root)
            payload = self.payload(operation='consume', root=str(root), requestId='environment_only',
                requiredOutputs=['generation','evaluation'], environmentRefs=[{'revision':'rev1'}], validationEpisodes=ids)
            with self.isolated_consumer():
                result = worker.consume_internal(payload, root)
            self.assertTrue(result['passed']); self.assertNotIn('demonstrations', result)
            self.assertEqual(result['evaluation']['interfaces'][0]['revision'], 'rev1')
            self.assertIn(result['evaluation']['interfaces'][0]['sourceEpisodeId'], ids)
            self.assertTrue(worker.required_files(payload, result))
            forged = deepcopy(result); forged['evaluation']['interfaces'][0]['sourceEpisodeId']='unknown'
            with self.assertRaises(ValueError): worker.required_files(payload, forged)

    def test_environment_interface_uses_failed_case_velocity_and_effort_actions(self):
        for mode in ('joint_velocity','joint_effort'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as name:
                root=Path(name);_,directory,manifest,_=self.context(root)
                port=manifest['io']['ports']['arm'];port['source']['primitive']=mode
                port['units']=['rad/s' if mode=='joint_velocity' else 'N*m']*port['shape'][0]
                port['persistence']='persistent' if mode=='joint_velocity' else 'instantaneous'
                manifest['compiled']['io']=deepcopy(manifest['io'])
                task=manifest['tasks'][0];task['parameters']['count']=999
                task['evaluation']['cases'][0]['expected']['success']=False
                manifest['sourceDigests'].pop('expert.py');(directory/'expert.py').unlink()
                ids=self.case_episodes(root)
                payload=self.payload(operation='consume',root=str(root),requestId='interface_'+mode,
                    requiredOutputs=['generation','evaluation'],environmentRefs=[{'revision':'rev1'}],validationEpisodes=ids)
                with self.isolated_consumer():result=worker.consume_internal(payload,root)
                self.assertTrue(result['passed']);self.assertTrue(worker.required_files(payload,result))
                interface=result['evaluation']['interfaces'][0]
                source=json.loads((root/'projects/p1/episodes'/interface['sourceEpisodeId']/'report.json').read_text())
                self.assertFalse(source['success'])
                probe=json.loads((root/interface['episodeRef']).read_text())
                self.assertEqual(probe['purpose'],'consumer_diagnostic');self.assertFalse(probe['candidateDemonstrationEligible'])

    def test_environment_without_actual_case_steps_cannot_invent_an_interface_action(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);_,directory,manifest,_=self.context(root)
            for case in manifest['tasks'][0]['evaluation']['cases']:
                case['expected']={'success':False,'reward':0.}
                (directory/case['sourceRef']).write_text(ZERO)
                manifest['sourceDigests'][case['sourceRef']]=hashlib.sha256(ZERO.encode()).hexdigest()
            ids=self.case_episodes(root)
            payload=self.payload(operation='consume',root=str(root),requestId='no_case_steps',
                requiredOutputs=['generation','evaluation'],environmentRefs=[{'revision':'rev1'}],validationEpisodes=ids)
            with self.assertRaisesRegex(ValueError,'actual executed steps'):worker.consume_internal(payload,root)

    def test_two_revision_environment_interfaces_and_parent_verification_keep_distinct_scopes(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);project,directory,manifest,world=self.context(root)
            import shutil
            other=project/'revisions/rev2';shutil.copytree(directory,other)
            second=deepcopy(manifest);second['revision']='rev2';second['artifacts']['manifest']='projects/p1/revisions/rev2/manifest.json'
            def frozen(_,payload):
                return project,other if payload.get('revision')=='rev2' else directory,second if payload.get('revision')=='rev2' else manifest,world
            with patch.object(worker,'frozen',side_effect=frozen):
                ids=self.case_episodes(root)+self.case_episodes(root,revision='rev2')
                payload=self.payload(operation='consume',root=str(root),requestId='two_environments',
                    requiredOutputs=['generation','evaluation'],environmentRefs=[{'revision':'rev1'},{'revision':'rev2'}],validationEpisodes=ids)
                with self.isolated_consumer():result=worker.consume_internal(payload,root)
                self.assertEqual({(row['revision'],row['taskId']) for row in result['evaluation']['interfaces']},
                    {('rev1','events'),('rev2','events')})
                self.assertTrue(worker.required_files(payload,result))
                for mutation in ('missing','swap','out_of_scope'):
                    changed=deepcopy(result)
                    if mutation=='missing':changed['evaluation']['interfaces'].pop()
                    elif mutation=='swap':changed['evaluation']['interfaces'][0]['episodeRef']=changed['evaluation']['interfaces'][1]['episodeRef']
                    else:changed['evaluation']['tasks'][0]['revision']='unknown'
                    worker.save(root/result['artifactRef'],{k:v for k,v in changed.items() if k!='artifactRef'})
                    with self.assertRaises(ValueError):worker.required_files(payload,changed)
                changed=deepcopy(payload);changed['validationEpisodes']=ids[:2]
                with self.assertRaisesRegex(ValueError,'validation'):worker.consume_internal(changed,root)

    def test_independent_behavior_changes_without_mutating_environment(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); project, directory, manifest, _ = self.context(root)
            manifest['capabilities']={'stageSplit':True,'independentBehavior':True}
            before = {p.name:p.read_bytes() for p in directory.iterdir()}
            results=[]
            for source in (TWO, TWO+'# independent change\n'):
                payload=self.payload(operation='rollout',root=str(root),jobId='fixture',behavior=self.candidate(source),repetitions=2)
                result=worker.rollout(payload,root); worker.required_files(payload,result); worker.finalize(payload,result)
                self.assertEqual(len(result['episodes']),2)
                self.assertTrue(all(row['demonstrationEligible'] for row in result['episodes']))
                self.assertTrue(all(row['revision']=='rev1' for row in result['episodes']))
                results.append(result)
            self.assertNotEqual(results[0]['episodes'][0]['behaviorRef'],results[1]['episodes'][0]['behaviorRef'])
            self.assertEqual({p.name:p.read_bytes() for p in directory.iterdir()},before)
            selection={'episodeId':results[0]['episode'],'taskId':'events','revision':'rev1','use':'successful_demonstration'}
            worker._behavior_data(root,project,directory,manifest,worker.frozen(root,{})[3],selection)
            behavior=root/results[0]['episodes'][0]['behaviorRef']; frozen=json.loads(behavior.read_text())
            (behavior.parent/frozen['sourceRef']).write_text('changed')
            with self.assertRaises(ValueError): worker.required_files(payload,results[0])

    def test_two_revision_dataset_keeps_same_task_separate_and_needs_no_evaluation(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); project,directory,manifest,world=self.context(root)
            import shutil
            other=project/'revisions/rev2';shutil.copytree(directory,other)
            second=deepcopy(manifest);second['revision']='rev2';second['artifacts']['manifest']='projects/p1/revisions/rev2/manifest.json'
            for build in (manifest,second):build['capabilities']={'stageSplit':True,'independentBehavior':True}
            def frozen(_,payload):
                return project, other if payload.get('revision')=='rev2' else directory, second if payload.get('revision')=='rev2' else manifest, world
            with patch.object(worker,'frozen',side_effect=frozen):
                selections=[]
                for revision in ('rev1','rev2'):
                    call=self.payload(operation='rollout',root=str(root),revision=revision,jobId='fixture',behavior=self.candidate(TWO))
                    run=worker.rollout(call,root);worker.finalize(call,run)
                    selections.append({'episodeId':run['episode'],'taskId':'events','revision':revision,'use':'successful_demonstration'})
                payload=self.payload(operation='consume',root=str(root),requestId='dataset',requiredOutputs=['demonstrations'],
                    environmentRefs=[{'revision':'rev1','split':'train','taskIds':['events']},{'revision':'rev2','split':'test','taskIds':['events']}],episodes=selections)
                with patch.object(worker,'validation_cases',side_effect=AssertionError('data-only must not evaluate')):
                    result=worker.consume_internal(payload,root);self.assertTrue(worker.required_files(payload,result))
                self.assertTrue(result['passed']);self.assertNotIn('evaluation',result);self.assertNotIn('reload',result)
                self.assertEqual([row['revision'] for row in result['demonstrations']['batches']],['rev1','rev2'])
                changed=deepcopy(payload);changed['episodes']=changed['episodes'][:1]
                with self.assertRaisesRegex(ValueError,'coverage|lack'):worker.consume_internal(changed,root)
                changed=deepcopy(payload);changed['environmentRefs'].append({'revision':'rev1','split':'test'})
                with self.assertRaises(ValueError):worker.consume_internal(changed,root)

    def test_external_low_score_is_valid_and_does_not_edit_environment_or_latest(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); project, directory, manifest, _ = self.context(root)
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            payload = self.payload(operation='evaluate', root=str(root), taskId='events', candidate=self.candidate(ZERO), requestId='low')
            result = worker.evaluate_internal(payload, root)
            self.assertEqual(result['executionStatus'], 'completed')
            self.assertFalse(result['result']['success'])
            self.assertFalse(result['demonstrationEligible'])
            self.assertTrue(worker.required_files(payload, result))
            self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, before)
            self.assertFalse((project/'latest_episode.json').exists())
            published = worker.finalize({**payload, 'jobId': 'published_candidate'}, result)
            self.assertEqual(json.loads((root/published['artifactRef']).read_text()), published)
            self.assertTrue(worker.required_files(payload, published))

    def test_parent_binds_candidate_summary_to_verified_episode_and_saved_report(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); self.context(root)
            payload = self.payload(operation='evaluate', root=str(root), taskId='events',
                candidate=self.candidate(ZERO), requestId='summary')
            result = worker.evaluate_internal(payload, root)
            report_path = root/result['artifactRef']
            self.assertTrue(worker.required_files(payload, result))
            for mutation in ('score', 'execution', 'availability', 'eligibility', 'artifact', 'saved'):
                with self.subTest(mutation=mutation):
                    changed = deepcopy(result)
                    changed['result'] = deepcopy(changed['result'])
                    if mutation == 'score': changed['result']['success'] = True
                    elif mutation == 'execution': changed['executionStatus'] = 'policy_error'
                    elif mutation == 'availability': changed['scoreStatus'] = 'unavailable'
                    elif mutation == 'eligibility': changed['demonstrationEligible'] = True
                    elif mutation == 'artifact': changed['artifactRef'] = 'projects/p1/evaluations/other/report.json'
                    saved = deepcopy(changed)
                    if mutation == 'saved': saved['result']['success'] = True
                    report_path.write_text(json.dumps(saved))
                    with self.assertRaisesRegex(ValueError, 'candidate.*(summary|report|scope|eligibility)'):
                        worker.required_files(payload, changed)

    def test_parent_rejects_candidate_error_relabelled_as_completed(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); self.context(root)
            payload = self.payload(operation='evaluate', root=str(root), taskId='events',
                candidate=self.candidate("def run(env, task):\n    raise RuntimeError('expected candidate error')\n"),
                requestId='status_wash')
            result = worker.evaluate_internal(payload, root)
            self.assertEqual(result['executionStatus'], 'policy_error')
            changed = deepcopy(result); changed['executionStatus'] = 'completed'
            (root/changed['artifactRef']).write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, 'candidate.*summary'):
                worker.required_files(payload, changed)

    def test_policy_exception_is_distinct_from_low_task_score(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); self.context(root)
            source = "def run(env, task):\n    raise RuntimeError('candidate crash')\n"
            result = worker.evaluate_internal(self.payload(taskId='events', candidate=self.candidate(source), requestId='crash'), root)
            self.assertEqual(result['executionStatus'], 'policy_error')
            self.assertFalse(result['result']['success'])
            self.assertIn('candidate crash', result['episodes'][0]['score']['error']['message'])

    def test_published_variable_cases_feed_check_and_parent_rejects_forged_state(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); project, directory, manifest, world = self.context(root)
            ids = []
            for id in ('two', 'zero'):
                source = 'case_'+id+'.py'
                payload = self.payload(operation='rollout', root=str(root), purpose='verification',
                    controller={'type': 'policy', 'policyRef': source, 'digest': manifest['sourceDigests'][source]})
                result = worker.rollout(payload, root)
                self.assertTrue(worker.required_files(payload, result))
                ids.append(result['episode'])
            checked = worker.check(self.payload(episodeIds=ids), root)
            self.assertTrue(checked['passed'])
            self.assertEqual([case['episodeId'] for case in checked['tasks'][0]['cases']], ids)
            episode = project/'episodes'/ids[0]
            rows, index, _ = read_transitions(episode)
            rows[0]['rubricState'] = {'count': 100}
            data = ''.join(json.dumps(row)+'\n' for row in rows).encode()
            (episode/'transitions.jsonl').write_bytes(data)
            index['committedBytes'] = len(data); (episode/'records.json').write_text(json.dumps(index))
            selected = worker.rubric_sources(Task(**manifest['tasks'][0]), worker.frozen_sources(directory, manifest), manifest['sourceDigests'])
            with self.assertRaisesRegex(worker.RubricUnavailable, 'reproduce'):
                worker.recompute_rubric(Task(**manifest['tasks'][0]), selected, episode)

    def test_check_without_actual_case_episodes_cannot_claim_semantic_validation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); self.context(root)
            checked = worker.check(self.payload(), root)
            self.assertFalse(checked['passed'])
            self.assertTrue(checked['tasks'][0]['bindingPassed'])
            self.assertTrue(all(case['result'] is None for case in checked['tasks'][0]['cases']))

    def test_passive_physics_cannot_be_published_as_a_robot_demonstration(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();project,directory,manifest,_=self.context(root)
            manifest['io']['ports']={};manifest['compiled']['io']=deepcopy(manifest['io'])
            source="def run(env, task):\n    env.step({})\n    env.step({})\n"
            (directory/'expert.py').write_text(source)
            manifest['sourceDigests']['expert.py']=hashlib.sha256(source.encode()).hexdigest()
            payload=self.payload(operation='rollout',root=str(root))
            result=worker.rollout(payload,root)
            self.assertTrue(result['score']['success'])
            self.assertGreater(result['score']['controlCycles'],0)
            self.assertEqual(result['executionKind'],'native_physics')
            self.assertFalse(result['demonstrationEligible'])
            self.assertTrue(worker.required_files(payload,result))
            path=project/'episodes'/result['episode']/'manifest.json'
            original=json.loads(path.read_text());forged=deepcopy(original)
            forged['candidateDemonstrationEligible']=True;path.write_text(json.dumps(forged))
            with self.assertRaisesRegex(ValueError,'control ports|actuator'):
                worker.required_files(payload,result)
            path.write_text(json.dumps(original))
            published=worker.finalize({**payload,'jobId':'passive_fixture'},result)
            self.assertFalse(published['demonstrationEligible'])

    def test_persistent_ports_can_hold_without_a_new_write_in_every_cycle(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();_,directory,manifest,_=self.context(root)
            source="def run(env, task):\n    env.step({})\n    env.step({})\n"
            (directory/'expert.py').write_text(source)
            manifest['sourceDigests']['expert.py']=hashlib.sha256(source.encode()).hexdigest()
            payload=self.payload(operation='rollout',root=str(root))
            result=worker.rollout(payload,root)
            self.assertTrue(result['score']['success'])
            self.assertTrue(result['demonstrationEligible'])
            self.assertTrue(worker.required_files(payload,result))

    def test_consumer_cannot_shorten_requested_demonstration_set(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); project,_,_,_ = self.context(root)
            _, _, manifest, _ = worker.frozen(root, {})
            from tests.test_native_records import IO
            manifest['io']['streams']['nodes'] = deepcopy(IO['streams']['nodes'])
            manifest['compiled']['io'] = deepcopy(manifest['io'])
            self._system_fixture_streams['nodes'] = [[float(index), 0., 1.] for index in range(70)]
            validation_ids = []
            for id in ('two', 'zero'):
                ref = 'case_'+id+'.py'
                payload = self.payload(operation='rollout', purpose='verification', jobId='fixture',
                    controller={'type': 'policy', 'policyRef': ref, 'digest': manifest['sourceDigests'][ref]})
                run = worker.rollout(payload, root); worker.finalize({**payload, 'root': str(root)}, run)
                validation_ids.append(run['episode'])
            demo_ids = []
            for _ in range(2):
                payload = self.payload(operation='rollout', jobId='fixture')
                run = worker.rollout(payload, root); worker.finalize({**payload, 'root': str(root)}, run)
                demo_ids.append(run['episode'])
            original = worker.run_controller
            def controller(*args, **kwargs):
                if kwargs.get('kind') == 'isolation':
                    return {'isolationChecks': {**{name:True for name,_,_ in kwargs['probes']},'networkBlocked':True}}
                return original(*args, **kwargs)
            payload = self.payload(operation='consume', root=str(root), episodes=[
                {'episodeId':id,'taskId':'events','use':'successful_demonstration'} for id in demo_ids],
                validationEpisodes=validation_ids, requestId='consumer')
            with patch.object(worker, 'run_controller', side_effect=controller):
                result = worker.consume_internal(payload, root)
            self.assertTrue(result['passed'])
            required = worker.required_files(payload, result)
            probe = root/result['evaluation']['interfaces'][0]['episodeRef'];probe=probe.parent
            arrays = {path.resolve() for path in probe.glob('arrays/*.npy')}
            self.assertTrue(arrays);self.assertTrue(arrays.issubset(required))
            annotation_path=probe/'annotations.json';original_annotations=json.loads(annotation_path.read_text())
            forged=deepcopy(original_annotations);forged['annotations']=[{'text':'uncommitted interpretation'}]
            annotation_path.write_text(json.dumps(forged))
            probe_manifest_path=probe/'manifest.json';probe_manifest=json.loads(probe_manifest_path.read_text())
            rehashed=deepcopy(probe_manifest);rehashed['artifactDigests']['annotations']=hashlib.sha256(annotation_path.read_bytes()).hexdigest()
            probe_manifest_path.write_text(json.dumps(rehashed))
            with self.assertRaisesRegex(ValueError,'annotations.*committed'):
                worker.required_files(payload,result)
            annotation_path.write_text(json.dumps(original_annotations))
            probe_manifest_path.write_text(json.dumps(probe_manifest))
            index_path=probe/'records.json';original_index=json.loads(index_path.read_text())
            for field in ('task','io'):
                with self.subTest(probe_contract=field):
                    altered=deepcopy(original_index)
                    if field == 'task':altered[field]['bindings']['object']='unrelated_node'
                    else:altered[field]['ports']['arm']['source']['joints'][0]='unrelated_joint'
                    index_path.write_text(json.dumps(altered))
                    try:
                        with self.assertRaises(ValueError):worker.required_files(payload,result)
                    finally:index_path.write_text(json.dumps(original_index))
            changed=deepcopy(result);changed['demonstrations']['batches'].pop();changed['demonstrations']['episodeCount']=1
            path=root/result['artifactRef'];path.write_text(json.dumps({k:v for k,v in changed.items()if k!='artifactRef'}))
            with self.assertRaisesRegex(ValueError,'behavior.*selection'):
                worker.required_files(payload,changed)

    def test_generated_rubric_recomputes_bulk_facts_from_only_referenced_array_bytes(self):
        import numpy as np
        from tests.test_native_records import IO
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);project,directory,manifest,world=self.context(root)
            manifest['io']['streams']['nodes']=deepcopy(IO['streams']['nodes'])
            manifest['compiled']['io']=deepcopy(manifest['io'])
            self._system_fixture_streams['nodes']=np.arange(210,dtype=np.float32).reshape(70,3).tolist()
            source="""import numpy as np
def evaluate(task,facts,state):
    sample=facts['initialSnapshot']['truth'] if facts['phase']=='reset' else facts['truthAfter']
    values=np.load(sample['streams']['nodes']['ref'],allow_pickle=False)
    return {'state':None,'result':{'success':False,'terminated':False,'reward':float(values.sum()),'details':{}}}
"""
            (directory/'rubrics.py').write_text(source)
            manifest['sourceDigests']['rubrics.py']=hashlib.sha256(source.encode()).hexdigest()
            result=worker.rollout(self.payload(),root);episode=project/'episodes'/result['episode']
            self.assertEqual(result['score']['executionStatus'],'completed',result['score'])
            self.assertEqual(result['score']['result']['reward'],21945.)
            rows,index,_=read_transitions(episode)
            self.assertNotIn('nodes',rows[0]['observation']['streams'])
            task=Task(**manifest['tasks'][0]);selected=worker.rubric_sources(task,worker.frozen_sources(directory,manifest),manifest['sourceDigests'])
            rebuilt=worker.recompute_rubric(task,selected,episode,io=manifest['io'])
            self.assertEqual(rebuilt['result'],result['score']['result'])
            reference=index['initialSnapshot']['truth']['streams']['nodes'];target=episode/reference['ref']
            target.write_bytes(target.read_bytes()[:-1]+b'X')
            with self.assertRaisesRegex(ValueError,'digest'):
                worker.recompute_rubric(task,selected,episode,io=manifest['io'])

    def test_camera_failure_private_arrays_remain_in_the_publication_closure(self):
        import numpy as np
        from runtime.records import _array_reference
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();project,_,_,_=self.context(root)
            payload=self.payload(operation='rollout',root=str(root))
            result=worker.rollout(payload,root);episode=project/'episodes'/result['episode']
            reference=_array_reference(episode,np.arange(210,dtype=np.float32).reshape(70,3))
            (episode/'camera-failure.json').write_text(json.dumps({
                'scope':'SYSTEM_VERIFICATION_ONLY private failure sample, not committed facts',
                'before':{'streams':{'uncommitted':reference}}}))
            required=worker.required_files(payload,result)
            self.assertIn(episode/reference['ref'],required)
            target=episode/reference['ref'];target.write_bytes(target.read_bytes()[:-1]+b'X')
            with self.assertRaisesRegex(ValueError,'digest'):
                worker.required_files(payload,result)

    def test_optional_camera_without_frames_keeps_empty_video_diagnostic_unregistered(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();project,_,manifest,_=self.context(root)
            manifest['tasks'][0]['recording']={'primaryCameraId':'optional','cameras':[{
                'cameraId':'optional','frame':'world','at':[0,0,1],'orientation':[1,0,0,0],
                'resolution':[32,32],'rateHz':25,'modalities':['rgb'],'video':True,
                'required':False,'source':'simulation_observer'}]}
            original=worker.load_environment.side_effect
            def load(*args,**kwargs):
                env=original(*args,**kwargs)
                env.camera_failures={'optional':'CPU sensor unavailable before its first actual frame'}
                video=env.directory/'videos/optional.mp4';video.parent.mkdir()
                video.write_bytes(b'SYSTEM_VERIFICATION_ONLY zero-frame encoder diagnostic, not delivered media')
                return env
            payload=self.payload(operation='rollout',root=str(root),purpose='verification',controller={
                'type':'policy','policyRef':'case_two.py','digest':manifest['sourceDigests']['case_two.py']})
            with patch.object(worker,'load_environment',side_effect=load):result=worker.rollout(payload,root)
            episode=project/'episodes'/result['episode'];receipt=json.loads((episode/'manifest.json').read_text())
            self.assertTrue((episode/'videos/optional.mp4').exists())
            self.assertNotIn('video:optional',receipt['artifacts']);self.assertNotIn('video',receipt['artifacts'])
            self.assertEqual(receipt['media']['cameras'][0]['frameCount'],0)
            self.assertIsNone(receipt['media']['cameras'][0]['video'])
            self.assertTrue(worker.required_files(payload,result))



    def test_declared_negative_case_rejects_constant_true_generated_rubric(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); _, directory, manifest, _ = self.context(root)
            source = "def evaluate(task, facts, state):\n    return {'state': None, 'result': {'success': True, 'terminated': False, 'reward': 0., 'details': {}}}\n"
            (directory/'rubrics.py').write_text(source)
            manifest['sourceDigests']['rubrics.py'] = hashlib.sha256(source.encode()).hexdigest()
            result = worker.rollout(self.payload(purpose='verification', controller={'type': 'policy',
                'policyRef': 'case_zero.py', 'digest': manifest['sourceDigests']['case_zero.py']}), root)
            checked = worker.check(self.payload(episodeIds=[result['episode']]), root)
            case = checked['tasks'][0]['cases'][1]
            self.assertTrue(case['result']['success'])
            self.assertFalse(case['expected']['success'])
            self.assertFalse(case['passed'])

    def test_unavailable_cycle_is_preserved_and_never_retried_as_a_valid_grade(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); project, directory, manifest, _ = self.context(root)
            source = SOURCE.replace("    count += int(facts['phase'] == 'transition')", "    if facts['phase'] == 'transition': raise RuntimeError('fixture scoring fault')\n    count += 0")
            (directory/'rubrics.py').write_text(source)
            manifest['sourceDigests']['rubrics.py'] = hashlib.sha256(source.encode()).hexdigest()
            payload = self.payload(operation='evaluate', root=str(root), taskId='events',
                candidate=self.candidate(TWO), requestId='ungraded')
            result = worker.evaluate_internal(payload, root)
            self.assertEqual(result['executionStatus'], 'score_unavailable')
            self.assertIsNone(result['result'])
            report = result['episodes'][0]['score']
            self.assertEqual(report['physicalSteps'], 10)
            self.assertIn('fixture scoring fault', str(report['scoreError']))
            self.assertTrue(worker.required_files(payload, result))

    def test_actual_uncommitted_tail_keeps_prefix_grade_but_no_final_model_score(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); self.context(root)
            original = worker.load_environment.side_effect
            def load(*args, **kwargs):
                env = original(*args, **kwargs)
                step = env.step
                def interrupted(action, seconds=.02):
                    if env.steps == 10:
                        env.steps += 3; env.time = env.steps/500
                        env._failed = {'countStatus':'known','totalExecutedPhysicalSteps':env.steps,
                            'actualClock':{key:env.observe()[key] for key in ('physicsStep','time','simulationTime','timeOrigin','stageId')},
                            'actualTail':env.export_state()}
                        from runtime.records import atomic_json
                        atomic_json(env.directory/'native-step-failure.json',env._failed)
                        raise OSError('fixture actual physical tail')
                    return step(action, seconds)
                env.step = interrupted
                return env
            payload = self.payload(operation='evaluate', root=str(root), taskId='events',
                candidate=self.candidate(TWO), requestId='tail')
            with patch.object(worker, 'load_environment', side_effect=load):
                result = worker.evaluate_internal(payload, root)
            report = result['episodes'][0]['score']
            self.assertEqual(result['executionStatus'], 'environment_error')
            self.assertIsNone(result['result'])
            self.assertIsNone(report['success'])
            self.assertEqual(report['lastCommittedResult']['reward'], 1.)
            self.assertEqual(report['uncommittedPhysicalSteps'], 3)
            with patch.object(worker, '_verify_media', side_effect=AssertionError('failed media is diagnostic only')):
                self.assertTrue(worker.required_files(payload, result))

    def test_external_expert_filename_uses_only_the_candidate_snapshot(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); _, directory, manifest, _ = self.context(root)
            candidate = {'sourceRef': 'expert.py', 'helperRefs': [], 'sourceFiles': {'expert.py': ZERO},
                         'sourceDigests': {'expert.py': hashlib.sha256(ZERO.encode()).hexdigest()}}
            payload = self.payload(operation='evaluate', root=str(root), taskId='events',
                candidate=candidate, requestId='same_filename')
            result = worker.evaluate_internal(payload, root)
            self.assertFalse(result['result']['success'])
            self.assertNotEqual(result['candidate']['sourceDigest'], manifest['sourceDigests']['expert.py'])
            self.assertEqual((directory/'expert.py').read_text(), TWO)
            self.assertTrue(worker.required_files(payload, result))


if __name__ == '__main__':
    unittest.main()
