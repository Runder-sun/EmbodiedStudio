from pathlib import Path
from types import SimpleNamespace
import sys
import time
import json
import os
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

from runtime.scene import Task, World
from runtime.worker import run_controller


class ControllerBudgetTests(unittest.TestCase):
    def environment(self, host_call):
        world = World()
        world.box('cube', (.04, .04, .04), mass=.06)
        task = Task('control_fixture', bindings={'object':'cube'}, rubric={'sourceRef':'rubrics.py','helperRefs':[]})
        task.bind(world)
        env = SimpleNamespace(world=world, time=0,
                              io={'clock':{'physicsRateHz':200,'controlRateHz':50}, 'ports':{}, 'streams':{}},
                              step=host_call, observe=lambda task, **kwargs: {'time':0}, compiled={})
        return env, task

    def test_frozen_profile_is_read_relative_to_real_expert_filename(self):
        env, task = self.environment(lambda *args: None)
        env.reset = lambda seed: None
        source = """from pathlib import Path
import json
def run(env, task):
    assert Path(__file__).name == 'expert.py'
    profile = json.loads(Path(__file__).with_name('arm.robot.json').read_text())
    assert profile['scope'] == 'SYSTEM_VERIFICATION_ONLY'
"""
        from runtime import worker
        with patch('runtime.worker.controller_command', return_value=[sys.executable, worker.__file__, '--controller']):
            result = run_controller(env, task, source=source,
                source_files={'expert.py': source, 'arm.robot.json': '{"scope":"SYSTEM_VERIFICATION_ONLY"}'},
                source_ref='expert.py', max_seconds=5)
        self.assertEqual(result['actionCalls'], 0)

    def test_generated_import_and_runtime_logs_do_not_become_controller_requests(self):
        command = Mock(return_value={'status': 'completed'})
        env, task = self.environment(command); env.reset = lambda seed: None
        source = '''import os
os.write(1, b'SYSTEM_VERIFICATION_ONLY native diagnostic\\n')
print("SYSTEM_VERIFICATION_ONLY import diagnostic", flush=True)
def run(env, task):
    print({'diagnostic': 'not a command'}, flush=True)
    print('{"action":"step","values":{"arm":[99]}}', flush=True)
    env.step({'arm': [0]}, seconds=.02)
'''
        from runtime import worker
        with patch('runtime.worker.controller_command', return_value=[sys.executable, worker.__file__, '--controller']):
            result = run_controller(env, task, source=source, source_files={'expert.py': source},
                source_ref='expert.py', max_seconds=5)
        self.assertEqual(result['actionCalls'], 1)
        command.assert_called_once_with({'arm': [0]}, .02)
        self.assertIn('import diagnostic', result['exitDiagnostics']['stderrTail'])
        self.assertIn('native diagnostic', result['exitDiagnostics']['stderrTail'])
        self.assertIn('not a command', result['exitDiagnostics']['stderrTail'])

    def test_default_step_accepts_one_complete_frozen_low_frequency_period(self):
        source="""import json,sys
json.loads(sys.stdin.readline())
print(json.dumps({'action':'step','values':{'arm':[0]}}),flush=True)
reply=json.loads(sys.stdin.readline())
if 'error' in reply:raise RuntimeError(reply['error'])
print(json.dumps({'action':'close'}),flush=True)
"""
        for rate in (1,5,10,50):
            command=Mock(return_value={});env,task=self.environment(command)
            env.io['clock']={'physicsRateHz':100,'controlRateHz':rate}
            with self.subTest(rate=rate),patch('runtime.worker.controller_command',return_value=[sys.executable,'-c',source]):
                result=run_controller(env,task,max_seconds=5)
                self.assertEqual(result['actionCalls'],1)
                command.assert_called_once_with({'arm':[0]},1/rate)

    def test_request_duration_is_strict_numeric_and_bounded_by_at_least_one_period(self):
        for rate in (1,5,50):
            maximum=max(.1,1/rate)
            requests=[('0.02',False),(True,False),(False,False),(0,False),(-.1,False),
                (float('nan'),False),(float('inf'),False),(maximum*2,False),(1/rate,True)]
            source="""import json,sys
json.loads(sys.stdin.readline())
for seconds,allowed in json.loads(%r):
 print(json.dumps({'action':'step','values':{'arm':[0]},'seconds':seconds}),flush=True)
 reply=json.loads(sys.stdin.readline())
 assert ('error' not in reply)==allowed,(seconds,reply)
print(json.dumps({'action':'close'}),flush=True)
"""%json.dumps(requests)
            command=Mock(return_value={});env,task=self.environment(command)
            env.io['clock']={'physicsRateHz':100,'controlRateHz':rate}
            with self.subTest(rate=rate),patch('runtime.worker.controller_command',return_value=[sys.executable,'-c',source]):
                result=run_controller(env,task,max_seconds=5)
                self.assertEqual(result['actionCalls'],1)
                command.assert_called_once_with({'arm':[0]},1/rate)

    def test_expert_and_independent_policy_use_named_step_without_host_planning_callbacks(self):
        for kind in ('expert', 'policy'):
            command=Mock(return_value={'status':'completed','reached':None})
            env,task=self.environment(command)
            source="""import json,sys
json.loads(sys.stdin.readline())
def request(message):
 print(json.dumps(message),flush=True)
 return json.loads(sys.stdin.readline())
opened=request({'action':'open'})
assert opened['result']['entrypoints']==['step']
for old in ('push','move_ee','move_ee_planned','set_gripper_width'):
 assert 'error' in request({'action':old})
reply=request({'action':'motion','goal':{'position':[.3,0,.6]},'constraints':{'mode':'plan'},'proof':True})
assert 'error' in reply
assert 'error' in request({'action':'motion','goal':{},'constraints':{}})
reply=request({'action':'step','values':{'arm':[.3]},'seconds':.02})
assert reply['result']['status']=='completed'
print(json.dumps({'action':'close'}),flush=True)
"""
            with self.subTest(kind=kind),patch('runtime.worker.controller_command',return_value=[sys.executable,'-c',source]):
                result=run_controller(env,task,kind=kind)
            self.assertEqual(result['actionCalls'],1)
            command.assert_called_once_with({'arm':[.3]}, .02)

    def test_controller_waiting_for_a_trusted_host_call_keeps_its_budget(self):
        clock = [0.]
        env, task = self.environment(lambda *args: clock.__setitem__(0, 200.))
        source = """import json,sys
json.loads(sys.stdin.readline())
print(json.dumps({'action':'step','values':{'arm':[0]},'seconds':.02}),flush=True)
reply=json.loads(sys.stdin.readline())
if 'error' in reply: raise RuntimeError(reply['error'])
print(json.dumps({'action':'close'}),flush=True)
"""
        with patch('runtime.worker.controller_command', return_value=[sys.executable, '-c', source]), \
             patch('runtime.worker.time.monotonic', side_effect=lambda: clock[0]):
            result = run_controller(env, task, max_seconds=.1)
        self.assertEqual(result['actionCalls'], 1)

    def test_rpc_feedback_does_not_modify_isolation_evidence_or_sensor_values(self):
        env, task = self.environment(lambda *args: None)
        observation = {'time': 0}
        env.observe = lambda task, **kwargs: observation
        env.last_result = {'success': False, 'terminated': False, 'reward': 0, 'details': {'private': True}}
        source = """import json,sys
json.loads(sys.stdin.readline())
def request(message):
 print(json.dumps(message),flush=True)
 return json.loads(sys.stdin.readline())['result']
request({'action':'open'})
reply=request({'action':'observe'})
assert reply['result']=={'success':False,'terminated':False,'reward':0}
assert reply['terminationPolicy']=='generated_rubric_at_committed_cycle'
request({'action':'isolation_result','checks':{'fileReadBlocked':True,'networkBlocked':True}})
print(json.dumps({'action':'close'}),flush=True)
"""
        with patch('runtime.worker.controller_command', return_value=[sys.executable, '-c', source]):
            result = run_controller(env, task, kind='isolation')
        self.assertEqual(result['isolationChecks'], {'fileReadBlocked': True, 'networkBlocked': True})
        self.assertEqual(observation, {'time': 0})

    def test_a_controller_cpu_hang_still_exhausts_the_wait_budget(self):
        env, task = self.environment(lambda *args: None)
        source = 'import sys;sys.stdin.readline()\nwhile True: pass'
        started = time.monotonic()
        with patch('runtime.worker.controller_command', return_value=[sys.executable, '-c', source]), \
             self.assertRaisesRegex(TimeoutError, 'controller.*budget'):
            run_controller(env, task, max_seconds=.05)
        self.assertLess(time.monotonic() - started, 2)

    def test_pre_close_timeout_retains_bounded_stderr_and_actual_call_counts(self):
        env, task = self.environment(lambda *args: None)
        source = """import json,sys,time
sys.stdin.readline()
print(json.dumps({'action':'observe'}),flush=True)
json.loads(sys.stdin.readline())
sys.stderr.write('x'*1000000+'INIT_STAGE_MARKER');sys.stderr.flush()
time.sleep(30)
"""
        with patch('runtime.worker.controller_command', return_value=[sys.executable, '-c', source]), \
             self.assertRaises(TimeoutError) as raised:
            run_controller(env, task, max_seconds=.3)
        notes = getattr(raised.exception, '__notes__', [])
        self.assertTrue(notes, 'timeout must preserve controller diagnostics')
        diagnostic = json.loads(notes[-1].split(': ', 1)[1])
        self.assertIn('INIT_STAGE_MARKER', diagnostic['stderrTail'])
        self.assertLessEqual(len(diagnostic['stderrTail'].encode()), 4000)
        self.assertEqual(diagnostic['observeCalls'], 1)
        self.assertEqual(diagnostic['actionCalls'], 0)
        self.assertEqual(diagnostic['controllerSeconds'], .3)
        self.assertGreaterEqual(diagnostic['computeSeconds'], .3)

    def test_partial_line_and_full_stderr_do_not_bypass_the_controller_budget(self):
        env, task = self.environment(lambda *args: None)
        sources = ("import sys,time;sys.stdin.readline();sys.stdout.write('{');sys.stdout.flush();time.sleep(2)",
                   "import sys,time;sys.stdin.readline();sys.stderr.write('x'*1000000);sys.stderr.flush();time.sleep(2)")
        for source in sources:
            started = time.monotonic()
            with self.subTest(source=source), patch('runtime.worker.controller_command', return_value=[sys.executable, '-c', source]), \
                 self.assertRaisesRegex(TimeoutError, 'controller.*budget'):
                run_controller(env, task, max_seconds=.05)
            self.assertLess(time.monotonic()-started, 1)

    def test_setup_and_reply_backpressure_keep_the_same_budget_and_reap_the_child(self):
        # An outer process group bounds the regression itself while testing a blocked pipe.
        runner = r'''
import json,sys,time
from unittest.mock import patch
from types import SimpleNamespace
from runtime.worker import run_controller
from runtime.scene import Task, World
import runtime.worker as worker
mode=sys.argv[1]
world=World();world.box('body',(.1,.1,.1))
task=Task('t',bindings={'object':'body'},rubric={'sourceRef':'rubrics.py','helperRefs':[]});task.bind(world)
env=SimpleNamespace(world=world,time=0,io={'clock':{'physicsRateHz':200,'controlRateHz':50},'ports':{},'streams':{}},
    observe=lambda *a,**k:{'time':0,'padding':'x'*4096})
child=('import time;time.sleep(10)' if mode=='setup' else
    'import sys,json,time;sys.stdin.readline()\nfor _ in range(10000): print(json.dumps({"action":"observe"}),flush=True)\ntime.sleep(10)')
launched=[];popen=worker.subprocess.Popen
def launch(*args,**kwargs):
    process=popen(*args,**kwargs);launched.append(process);return process
started=time.monotonic()
with patch.object(worker,'controller_command',return_value=[sys.executable,'-c',child]), \
     patch.object(worker.subprocess,'Popen',side_effect=launch):
    try:run_controller(env,task,source=' '*262144 if mode=='setup' else None,max_seconds=.05)
    except TimeoutError:pass
    else:raise AssertionError('backpressure escaped the controller budget')
assert time.monotonic()-started<1
assert len(launched)==1 and launched[0].poll() is not None
print(json.dumps({'mode':mode,'reaped':True}))
'''
        for mode in ('setup', 'reply'):
            process = subprocess.Popen([sys.executable, '-c', runner, mode], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                with self.subTest(mode=mode):
                    try:
                        stdout, stderr = process.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                        self.fail('controller blocked on stdin beyond its wall-clock budget')
                    self.assertEqual(process.returncode, 0, stdout+stderr)
                    self.assertEqual(json.loads(stdout), {'mode': mode, 'reaped': True})
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()

    def test_large_frozen_setup_survives_partial_nonblocking_writes(self):
        env, task = self.environment(lambda *args: None)
        source = "import json,sys;setup=json.loads(sys.stdin.readline());assert len(setup['source'])==262144;print(json.dumps({'action':'close'}),flush=True)"
        with patch('runtime.worker.controller_command', return_value=[sys.executable, '-c', source]):
            result = run_controller(env, task, source=' '*262144, max_seconds=1)
        self.assertEqual(result['actionCalls'], 0)

    def test_controller_budget_is_cumulative_across_requests(self):
        env, task = self.environment(lambda *args: None)
        source = """import json,sys,time
json.loads(sys.stdin.readline())
for _ in range(20):
    time.sleep(.1)
    print(json.dumps({'action':'observe'}),flush=True)
    json.loads(sys.stdin.readline())
print(json.dumps({'action':'close'}),flush=True)
"""
        with patch('runtime.worker.controller_command', return_value=[sys.executable, '-c', source]), \
             self.assertRaisesRegex(TimeoutError, 'controller.*budget'):
            run_controller(env, task, max_seconds=.3)

    def test_close_drains_large_stdout_stderr_and_mixed_exit_logs_without_more_actions(self):
        for channels in ((1,), (2,), (1,2)):
            with self.subTest(channels=channels):
                command=Mock();env,task=self.environment(command)
                source="""import atexit,json,os,sys
sys.stdin.readline()
def logs():
 print(json.dumps({'action':'step','values':{'arm':[.1]},'seconds':.02}),flush=True)
 for channel in CHANNELS:os.write(channel,b'x'*(1024*1024))
atexit.register(logs)
print(json.dumps({'action':'close'}),flush=True)
""".replace('CHANNELS',repr(channels))
                from runtime import worker
                with patch.object(worker,'controller_command',return_value=[sys.executable,'-B','-c',source]), \
                     patch.object(worker,'flush_performance') as diagnostic:
                    result=run_controller(env,task,kind='expert')
                self.assertEqual(result['actionCalls'],0);self.assertEqual(result['actionAttempts'],0);command.assert_not_called()
                self.assertEqual(result['exitDiagnostics']['exitCode'],0);self.assertTrue(result['exitDiagnostics']['pipesClosed'])
                for name in ('stdoutTail','stderrTail'):
                    self.assertLessEqual(len(result['exitDiagnostics'][name].encode()),4000)
                self.assertTrue(diagnostic.called,'controller exit must retain phase diagnostics')
                for call in diagnostic.call_args_list:
                    self.assertLess(len(json.dumps({'args':call.args,'kwargs':call.kwargs})),16000)

    def test_normal_post_close_cleanup_longer_than_five_seconds_keeps_original_budget(self):
        from runtime import worker
        env,task=self.environment(Mock())
        source="""import atexit,json,sys,time
sys.stdin.readline()
atexit.register(lambda:time.sleep(5.25))
print(json.dumps({'action':'close'}),flush=True)
"""
        with patch.object(worker,'controller_command',return_value=[sys.executable,'-B','-c',source]):
            result=run_controller(env,task,kind='isolation',max_seconds=8)
        self.assertEqual(result['exitDiagnostics']['exitCode'],0);self.assertTrue(result['exitDiagnostics']['pipesClosed'])
        self.assertGreater(result['exitDiagnostics']['waitSeconds'],5)
        self.assertGreater(result['exitDiagnostics']['remainingSecondsAtClose'],5)
        self.assertLess(result['exitDiagnostics']['waitSeconds'],8)

    def test_close_does_not_restart_a_nearly_spent_controller_budget(self):
        from runtime import worker
        env,task=self.environment(Mock())
        source="""import atexit,json,sys,time
sys.stdin.readline()
atexit.register(lambda:time.sleep(.3))
time.sleep(.15)
print(json.dumps({'action':'close'}),flush=True)
"""
        started=time.monotonic()
        with patch.object(worker,'controller_command',return_value=[sys.executable,'-B','-c',source]), \
             self.assertRaises(worker.ControllerExecutionError) as raised:
            run_controller(env,task,kind='isolation',max_seconds=.25)
        self.assertEqual(raised.exception.execution_status,'policy_error')
        self.assertLess(time.monotonic()-started,1)

    def test_diagnostic_and_drain_time_cannot_be_excluded_after_budget_is_spent(self):
        from runtime import worker
        env,task=self.environment(Mock())
        source="""import atexit,json,sys,time
sys.stdin.readline()
atexit.register(lambda:time.sleep(.3))
print(json.dumps({'action':'close'}),flush=True)
"""
        def diagnostic(phase,*args,**kwargs):
            if phase=='controller:waiting_exit':time.sleep(.2)
        started=time.monotonic()
        with patch.object(worker,'controller_command',return_value=[sys.executable,'-B','-c',source]), \
             patch.object(worker,'flush_performance',side_effect=diagnostic), \
             self.assertRaises(worker.ControllerExecutionError):
            run_controller(env,task,kind='isolation',max_seconds=.1)
        self.assertLess(time.monotonic()-started,1)

    def test_post_close_output_flood_cannot_refresh_remaining_budget(self):
        from runtime import worker
        env,task=self.environment(Mock())
        source="""import atexit,json,os,sys
sys.stdin.readline()
def flood():
 while True:
  os.write(1,b'x'*4096)
  os.write(2,b'y'*4096)
atexit.register(flood)
print(json.dumps({'action':'close'}),flush=True)
"""
        started=time.monotonic()
        with patch.object(worker,'controller_command',return_value=[sys.executable,'-B','-c',source]), \
             self.assertRaises(worker.ControllerExecutionError):
            run_controller(env,task,kind='isolation',max_seconds=.2)
        self.assertLess(time.monotonic()-started,1)

    def test_nonzero_exit_after_large_logs_remains_a_policy_error_with_bounded_tail(self):
        from runtime import worker
        env,task=self.environment(Mock())
        source="""import atexit,json,os,sys
sys.stdin.readline()
atexit.register(lambda:os.write(2,b'x'*(1024*1024)+b'EXIT_TAIL_MARKER'))
print(json.dumps({'action':'close'}),flush=True)
raise SystemExit(7)
"""
        with patch.object(worker,'controller_command',return_value=[sys.executable,'-B','-c',source]), \
             self.assertRaises(worker.ControllerExecutionError) as raised:
            run_controller(env,task,kind='isolation')
        self.assertEqual(raised.exception.execution_status,'policy_error')
        self.assertIn('EXIT_TAIL_MARKER',str(raised.exception));self.assertLess(len(str(raised.exception)),5000)

    def test_close_does_not_turn_a_real_exit_hang_into_success_or_extend_remaining_budget(self):
        from runtime import worker
        env,task=self.environment(Mock());launched=[];popen=subprocess.Popen
        def launch(*args,**kwargs):
            process=popen(*args,**kwargs);launched.append(process);return process
        source="import json,sys,time;sys.stdin.readline();print(json.dumps({'action':'close'}),flush=True);time.sleep(30)"
        started=time.monotonic()
        with patch.object(worker,'controller_command',return_value=[sys.executable,'-B','-c',source]), \
             patch.object(worker.subprocess,'Popen',side_effect=launch), \
             self.assertRaises((TimeoutError,subprocess.TimeoutExpired,worker.ControllerExecutionError)):
            run_controller(env,task,kind='isolation',max_seconds=.2)
        self.assertLess(time.monotonic()-started,1);self.assertEqual(len(launched),1)
        self.assertIsNotNone(launched[0].poll())

    def test_environment_failure_is_not_masked_by_exit_timeout(self):
        from runtime import worker
        env,task=self.environment(Mock());env.observe=Mock(side_effect=RuntimeError('original environment failure'))
        source="""import json,sys,time
sys.stdin.readline()
print(json.dumps({'action':'observe'}),flush=True)
assert 'original environment failure' in json.loads(sys.stdin.readline())['error']
print(json.dumps({'action':'close'}),flush=True)
time.sleep(30)
"""
        started=time.monotonic()
        with patch.object(worker,'controller_command',return_value=[sys.executable,'-B','-c',source]), \
             self.assertRaises(worker.ControllerExecutionError) as raised:
            run_controller(env,task,kind='expert',max_seconds=.2)
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(raised.exception.execution_status,'environment_error')
        self.assertIn('original environment failure',str(raised.exception))

    @unittest.skipUnless(hasattr(os,'fork'),'pipe inheritance probe needs POSIX fork')
    def test_descendant_retaining_exit_pipes_is_bounded_and_cannot_be_success(self):
        runner=r'''
import json,os,sys,time
from unittest.mock import patch
from types import SimpleNamespace
from runtime import worker
child="""import json,os,sys,time
sys.stdin.readline()
if os.fork()==0:
 time.sleep(30)
 os._exit(0)
print(json.dumps({'action':'close'}),flush=True)
raise SystemExit(EXIT_CODE)
""".replace('EXIT_CODE',sys.argv[1])
env=SimpleNamespace(world=SimpleNamespace(seed=0))
task=SimpleNamespace(id='SYS_EXIT',instruction='SYSTEM_VERIFICATION_ONLY',bindings={},parameters={})
with patch.object(worker,'controller_command',return_value=[sys.executable,'-B','-c',child]):
 started=time.monotonic()
 try:worker.run_controller(env,task,kind='isolation',max_seconds=.2)
 except (TimeoutError,__import__('subprocess').TimeoutExpired,worker.ControllerExecutionError):pass
 else:raise AssertionError('open inherited output pipes cannot qualify a finished controller')
 assert time.monotonic()-started<1,'exit obtained extra time after its controller budget'
print(json.dumps({'boundedFailure':True}))
'''
        for code in (0,7):
            with self.subTest(exitCode=code):
                process=subprocess.Popen([sys.executable,'-B','-c',runner,str(code)],stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE,text=True,start_new_session=True)
                try:
                    try:stdout,stderr=process.communicate(timeout=8)
                    except subprocess.TimeoutExpired:self.fail('inherited pipe exit exceeded the bounded shutdown period')
                    self.assertEqual(process.returncode,0,stdout+stderr)
                    self.assertEqual(json.loads(stdout),{'boundedFailure':True})
                finally:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.communicate()

    def test_isolation_probe_has_no_robot_state_or_actuator_access(self):
        env,task=self.environment(Mock())
        env.record=False
        env.observe=Mock(side_effect=AssertionError('filesystem isolation does not consume SDK arrays'))
        source="""import json,sys
json.loads(sys.stdin.readline())
def request(value):
 print(json.dumps(value),flush=True)
 return json.loads(sys.stdin.readline())
opened=request({'action':'open'})['result']
assert opened['observation']=={} and opened['entrypoints']==[]
assert request({'action':'observe'})['result']['terminated']==False
assert 'error' in request({'action':'step','values':{},'seconds':.02})
request({'action':'isolation_result','checks':{'fixture':True}})
print(json.dumps({'action':'close'}),flush=True)
"""
        with patch('runtime.worker.controller_command',return_value=[sys.executable,'-c',source]):
            result=run_controller(env,task,kind='isolation')
        self.assertEqual(result['isolationChecks'],{'fixture':True})
        env.observe.assert_not_called()
        env.step.assert_not_called()

    def test_formal_controller_requires_the_committed_recorder_before_launch(self):
        env,task=self.environment(Mock())
        env.record=False
        with patch('runtime.worker.subprocess.Popen',side_effect=AssertionError('must reject before launch')) as launch:
            with self.assertRaisesRegex(ValueError,'record'):
                run_controller(env,task,kind='expert')
        launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
