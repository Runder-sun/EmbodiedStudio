"""SYSTEM_VERIFICATION_ONLY: CPU contracts, never generated scene products."""
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from runtime import robot, simulation, worker, motion
from runtime.records import Recorder
from runtime.scene import Task, World

HOLD = [0, -.4, 0, -2, 0, 1.6, .7854, .08]


class RuntimeSimplificationTests(unittest.TestCase):
    def test_whole_named_step_rejects_budget_before_native_work(self):
        from tests.test_native_environment import NativeEnvironmentTests
        fixture=NativeEnvironmentTests();self.addCleanup(fixture.doCleanups)
        env,art,modules=fixture.step_fixture()
        with patch.dict(__import__('sys').modules,modules):
            with self.assertRaisesRegex(ValueError,'budget'):env.step({'motor':[0]},.06)
        self.assertEqual(art.calls,[])
        self.assertEqual(env.steps,0)


    def test_zero_confirmed_tick_failure_cannot_be_cleared_by_reset(self):
        from tests.test_native_environment import NativeEnvironmentTests
        fixture=NativeEnvironmentTests();self.addCleanup(fixture.doCleanups)
        env,art,modules=fixture.step_fixture()
        env.world=SimpleNamespace(seed=0)
        env._failed={'confirmedStepLowerBound':0,'error':'native attempt failed before a confirmed tick'}
        with self.assertRaisesRegex(ValueError,'fresh native episode'):env.reset(0)
        self.assertEqual(env._failed['confirmedStepLowerBound'],0)
        self.assertEqual(art.calls,[])


    def test_throwing_native_tick_keeps_observed_partial_count_and_primary_failure(self):
        import json,sys
        from tests.test_native_environment import NativeEnvironmentTests
        fixture=NativeEnvironmentTests();self.addCleanup(fixture.doCleanups)
        env,art,modules=fixture.step_fixture();env.world=SimpleNamespace(seed=0)
        manager=modules['isaacsim.core.simulation_manager'].SimulationManager
        advance=manager.step.side_effect
        def partial(**kwargs):
            advance(**kwargs)
            raise RuntimeError('original native drive failure')
        manager.step.side_effect=partial
        with patch.dict(sys.modules,modules):
            with self.assertRaisesRegex(RuntimeError,'original native drive failure'):env.step({'motor':[.2]},.02)
            with self.assertRaisesRegex(ValueError,'ended or failed'):env.step({'motor':[0]},.02)
            with self.assertRaisesRegex(ValueError,'fresh native episode'):env.reset(0)
        report=json.loads((env.directory/'native-step-failure.json').read_text())
        self.assertEqual(report['totalExecutedPhysicalSteps'],1)
        self.assertEqual(report['uncommittedPhysicalSteps'],1)
        self.assertEqual(report['committedCycles'],0)
        self.assertEqual(report['countStatus'],'known')
        self.assertEqual(report['error']['message'],'original native drive failure')

    def test_unreadable_failure_clock_does_not_replace_primary_or_invent_zero_execution(self):
        import json,sys
        from tests.test_native_environment import NativeEnvironmentTests
        fixture=NativeEnvironmentTests();self.addCleanup(fixture.doCleanups)
        env,art,modules=fixture.step_fixture()
        original_clock=env._clock.side_effect
        calls=[0]
        def clock():
            calls[0]+=1
            if calls[0]>1:raise OSError('actual clock unavailable')
            return original_clock()
        env._clock.side_effect=clock
        modules['isaacsim.core.simulation_manager'].SimulationManager.step.side_effect=RuntimeError('original attempted tick failed')
        with patch.dict(sys.modules,modules),self.assertRaisesRegex(RuntimeError,'original attempted tick failed'):
            env.step({'motor':[.2]},.02)
        report=json.loads((env.directory/'native-step-failure.json').read_text())
        self.assertIsNone(report['totalExecutedPhysicalSteps'])
        self.assertIsNone(report['uncommittedPhysicalSteps'])
        self.assertEqual(report['countStatus'],'unknown')
        self.assertEqual(report['confirmedStepLowerBound'],0)
        self.assertEqual(report['error']['message'],'original attempted tick failed')

    def test_task_evaluation_rejects_reserved_and_escaping_source_paths(self):
        world = World()
        world.box('cube', (.04, .04, .04), mass=.06)
        for source in ('expert.py','../policy.py','scene.py'):
            task=Task('t',bindings={'object':'cube'},rubric={'sourceRef':'rubrics.py','helperRefs':[]},
                evaluation={'cases':[{'id':'case','sourceRef':source,'helperRefs':[],
                    'expected':{'success':False},'requirementRefs':['fixture']}]})
            with self.subTest(source=source), self.assertRaises(ValueError):
                task.bind(world)

    def test_frozen_policy_closure_digest_and_imports_are_checked(self):
        files = {'scene.py': '', 'tasks.py': '', 'expert.py': 'private = True',
                 'policy.py': 'from helper import run\n', 'helper.py': 'def run(env, task): pass\n',
                 'negative.py': 'def run(env, task): pass\n'}
        task=Task('t',rubric={'sourceRef':'rubrics.py','helperRefs':[]},evaluation={'cases':[
            {'id':'policy','sourceRef':'policy.py','helperRefs':['helper.py'],
             'expected':{'success':True},'requirementRefs':['fixture']},
            {'id':'negative','sourceRef':'negative.py','helperRefs':[],
             'expected':{'success':False},'requirementRefs':['fixture']}]})
        digests = {name: hashlib.sha256(source.encode()).hexdigest() for name, source in files.items()}
        selected = worker.evaluation_sources(task, files, digests)
        self.assertEqual(set(selected['policy']['sourceFiles']), {'policy.py', 'helper.py'})
        self.assertNotIn('expert.py', selected['policy']['sourceFiles'])
        for changed in (dict(files, **{'policy.py': 'import expert\n'}),
                        dict(files, **{'helper.py': 'import negative\n'}),
                        dict(files, **{'helper.py': 'changed = True\n'})):
            with self.assertRaises(ValueError):
                worker.evaluation_sources(task, changed, digests)

    def test_committed_records_exclude_unpublished_truth(self):
        from runtime.records import read_transitions
        from tests.test_native_records import begin,observation,COMMAND
        with tempfile.TemporaryDirectory() as temporary:
            recorder,_=begin(temporary)
            recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10,
                truth_before=observation(0,truth=True),truth_after=observation(.02,truth=True))
            before,index,_=read_transitions(temporary)
            with patch('runtime.records.atomic_json',side_effect=OSError('unpublished index')):
                with self.assertRaises(OSError):
                    recorder.commit(observation(.02),COMMAND,COMMAND,observation(.04),10,
                        truth_before=observation(.02,truth=True),truth_after=observation(.04,truth=True))
            recorder.close('interrupted')
            rows,_,tail=read_transitions(temporary)
            self.assertEqual(rows,before)
            self.assertEqual(recorder.committed_observation,observation(.02))
            self.assertNotIn('nodes',recorder.committed_observation['streams'])
            self.assertTrue(tail)



if __name__ == '__main__':
    unittest.main()
