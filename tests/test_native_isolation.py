"""SYSTEM_VERIFICATION_ONLY: native resource and controller process boundaries.

Actual Linux checks require SCENEAGENT_NATIVE_ISOLATION_ROOT under the independent
n6 diagnostics tree. They never open a Studio project, start Kit or submit tasks.
"""
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest
from unittest.mock import patch

from runtime import worker


class NativeIsolationCommandTests(unittest.TestCase):
    def test_gpu_compute_requires_an_explicit_job_assignment(self):
        with patch('runtime.worker.shutil.which', return_value='/usr/bin/bwrap'):
            for env in ({}, {'SCENEAGENT_GPU': '3'}, {'SCENEAGENT_GPU': '3', 'CUDA_VISIBLE_DEVICES': '0,1'}):
                with self.subTest(env=env), patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(ValueError, 'assigned'):
                    worker.controller_command(gpu=True)


@unittest.skipUnless(os.environ.get('SCENEAGENT_NATIVE_ISOLATION_ROOT'), 'explicit Linux system verification only')
class NativeIsolationLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = Path(os.environ['SCENEAGENT_NATIVE_ISOLATION_ROOT']).resolve()
        if '/sceneagent-studio-data/n6/diagnostics/' not in str(cls.directory):
            raise ValueError('isolation verification must stay under independent n6 diagnostics')
        cls.directory.mkdir(parents=True, exist_ok=True)
        cls.n6 = next(path for path in cls.directory.parents if path.name == 'n6')
        cls.main_root = cls.n6.parent

    def save(self, name, value):
        (self.directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    def command(self, code, **options):
        command = worker.controller_command(**options)
        return [*command[:-3], sys.executable, '-c', code]

    def test_actual_build_inputs_have_no_output_write_alias(self):
        output = self.directory/'build-mount-fixture'
        inputs, resources = output/'build-inputs', output/'dependencies/resources'
        inputs.mkdir(parents=True, exist_ok=True)
        resources.mkdir(parents=True, exist_ok=True)
        (inputs/'scene.py').write_text('# original source\n')
        (resources/'weights.bin').write_bytes(b'original weights')
        base = worker.controller_command('--native-build-source')
        with patch.object(worker, 'controller_command', return_value=base):
            command = worker.native_build_command(inputs, output, self.n6, self.directory/'no-legacy')
        code = '''import json
from pathlib import Path
result={}
for name in ('/inputs/scene.py','/output/build-inputs/scene.py','/output/dependencies/resources/weights.bin'):
 try:Path(name).write_bytes(b'overwrite')
 except OSError:result[name]=True
 else:result[name]=False
Path('/output/allowed.txt').write_text('output remains writable')
print(json.dumps(result))
'''
        completed = subprocess.run(command[:-3]+[sys.executable, '-c', code], capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        checks = json.loads(completed.stdout)
        self.save('build-readonly-alias-probe.json', {'checks':checks, 'passed':all(checks.values()),
            'scope':'SYSTEM_VERIFICATION_ONLY real bwrap aliases; no SDK or product'})
        self.assertTrue(all(checks.values()), checks)
        self.assertEqual((inputs/'scene.py').read_text(), '# original source\n')
        self.assertEqual((resources/'weights.bin').read_bytes(), b'original weights')
        self.assertTrue((output/'allowed.txt').is_file())

    def test_actual_asset_source_freezes_only_declared_native_files(self):
        source = '''# SYSTEM_VERIFICATION_ONLY: resource bytes, never a formal scene or policy.
from pathlib import Path

def build_asset(materials):
    try:
        Path('/inputs/sources/asset.py').write_text('overwrite')
    except OSError:
        pass
    else:
        raise AssertionError('frozen source mount was writable')
    Path('/output/model.usda').write_text('#usda 1.0\\ndef Xform "Verification" {}\\n')
    Path('/output/weights.pt').write_bytes(b'opaque SYSTEM_VERIFICATION_ONLY fixture')
    return {'entry': 'model.usda', 'files': ['model.usda', 'weights.pt']}
'''
        files = {'scene.py': '# SYSTEM_VERIFICATION_ONLY', 'tasks.py': '# SYSTEM_VERIFICATION_ONLY',
                 'expert.py': '# SYSTEM_VERIFICATION_ONLY', 'asset.py': source}
        source_hash = hashlib.sha256(json.dumps(files, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        root = self.directory / 'asset-root'
        report = worker.generate_asset({'sourceFile': 'asset.py', 'sourceFiles': files,
            'sourceHash': source_hash, 'expectedSourceHash': source_hash}, self.n6 / 'missing-legacy', root)
        from runtime.assets import _cache_valid
        self.assertEqual(_cache_valid(Path(report['reportPath']).parent), report)
        self.assertEqual(report['kind'], 'native_files')
        self.assertEqual(set(report['nativeFiles']), {'files/model.usda', 'files/weights.pt'})
        self.assertEqual(list((root / 'assets/generated').glob('generating-*')), [])
        self.save('asset-probe.json', {'passed': True, 'sourceHash': source_hash, 'report': report,
                                      'sdkStarted': False, 'gpuUsed': False})

    def test_actual_controller_gpu_cache_and_host_write_boundaries(self):
        import fcntl
        lock = self.main_root / 'runtime/isaac-gpu.lock'
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            from runtime.jobs import assign_device
            environment = {}
            assign_device({'index': 3, 'uuid': 'GPU-30000000-0000-0000-0000-000000000000'}, environment)
            marker = 'SYSTEM_VERIFICATION_ONLY-no-host-write-' + str(os.getpid())
            main_target = self.main_root / marker
            prefix_target = Path(sys.prefix) / marker
            self.assertFalse(main_target.exists())
            self.assertFalse(prefix_target.exists())
            code = '''import glob,json,os,pathlib,sys
import torch
cache={}
for name in ('HOME','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','WARP_CACHE_PATH','TORCH_EXTENSIONS_DIR','CUDA_CACHE_PATH'):
    path=pathlib.Path(os.environ[name]);assert str(path).startswith('/tmp/')
    path.mkdir(parents=True,exist_ok=True);(path/'test').write_text('private namespace')
    cache[name]=str(path)
assert torch.cuda.is_available()
assert torch.cuda.device_count()==1
values=torch.arange(1,1025,device='cuda',dtype=torch.float32)
actual=(values*2).sum().item();torch.cuda.synchronize()
assert actual==1049600.0,actual
write={}
for name,value in json.loads(sys.argv[1]).items():
    try:pathlib.Path(value).write_text('namespace write attempt')
    except OSError as error:write[name]={'denied':True,'error':type(error).__name__}
    else:write[name]={'denied':False,'scope':'private namespace only; host checked by parent'}
print(json.dumps({'cudaCount':torch.cuda.device_count(),'cudaName':torch.cuda.get_device_name(0),'sum':actual,
 'devices':sorted(glob.glob('/dev/nvidia[0-9]*')),'cache':cache,'writes':write,'pidNamespace':os.readlink('/proc/self/ns/pid')}))
'''
            with patch.dict(os.environ, environment):
                command = self.command(code, gpu=True)
            command += [json.dumps({'prefix': str(prefix_target), 'mainRoot': str(main_target)})]
            process = subprocess.run(command, text=True, capture_output=True, timeout=90)
            self.assertEqual(process.returncode, 0, process.stderr[-6000:])
            report = json.loads(process.stdout.splitlines()[-1])
            self.assertEqual(report['devices'], ['/dev/nvidia3'])
            self.assertTrue(report['writes']['prefix']['denied'])
            self.assertFalse(main_target.exists())
            self.assertFalse(prefix_target.exists())
            report.update(passed=True, hostPrefixUnchanged=True, hostMainRootUnchanged=True,
                          lockPath=str(lock.resolve()), lockInode=os.fstat(handle.fileno()).st_ino, sdkStarted=False)
            self.save('controller-gpu-probe.json', report)

    def test_actual_namespace_exit_terminates_detached_descendants(self):
        def token(pid):
            try:
                data = Path('/proc') / str(pid) / 'stat'
                text = data.read_text().split(') ', 1)[1].split()
                return text[19], text[0]
            except (FileNotFoundError, ProcessLookupError):
                return None
        def descendants(pid):
            pending, result = [pid], {}
            while pending:
                current = pending.pop()
                value = token(current)
                if value is None or current in result:
                    continue
                result[current] = value
                try:
                    children = (Path('/proc') / str(current) / 'task' / str(current) / 'children').read_text().split()
                except FileNotFoundError:
                    continue
                pending.extend(map(int, children))
            return result
        code = '''import json,os,subprocess,sys
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)'],start_new_session=True,
 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(json.dumps({'leader':os.getpid(),'detachedChild':child.pid}),flush=True)
sys.stdin.readline()
os._exit(0)
'''
        process = subprocess.Popen(self.command(code), text=True, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            ready, _, _ = select.select([process.stdout], [], [], 20)
            self.assertTrue(ready, 'isolated process did not reach descendant barrier')
            report = json.loads(process.stdout.readline())
            before = descendants(process.pid)
            self.assertGreaterEqual(len(before), 3)
            process.stdin.write('\n')
            process.stdin.flush()
            process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0)
            live = {pid: current for pid, value in before.items()
                    if (current := token(pid)) is not None and current[0] == value[0] and current[1] != 'Z'}
            self.assertEqual(live, {})
            self.save('controller-descendants-probe.json', {'passed': True, 'namespacePids': report,
                'hostProcessTokensBefore': before, 'liveDescendantsAfter': live, 'returncode': process.returncode,
                'scope': 'same PID namespace; detached session child; no SDK or generated product'})
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
