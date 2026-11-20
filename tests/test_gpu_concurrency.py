"""Isolated CPU/process fixtures for GPU admission; never generation products."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from runtime import jobs

A={'index':0,'uuid':'GPU-00000000-0000-0000-0000-000000000000'}
B={'index':2,'uuid':'GPU-22222222-2222-2222-2222-222222222222'}
class GPUAdmissionTests(unittest.TestCase):
    def child(self,root,device):
        source="from pathlib import Path\nimport json\nfrom runtime.jobs import worker_gpu_lock\nwith worker_gpu_lock(Path("+repr(str(root))+"),json.loads("+repr(json.dumps(device))+")):\n print('admitted',flush=True)\n"
        p=subprocess.Popen([sys.executable,'-c',source],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda:p.kill() if p.poll() is None else None)
        return p
    def test_different_gpus_can_enter_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            with jobs.worker_gpu_lock(Path(tmp),A):
                p=self.child(tmp,B);out,err=p.communicate(timeout=4)
                self.assertEqual(p.returncode,0,err);self.assertIn('admitted',out)
    def test_same_gpu_still_serializes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with jobs.worker_gpu_lock(Path(tmp),A):
                p=self.child(tmp,A)
                with self.assertRaises(subprocess.TimeoutExpired):p.communicate(timeout=.2)
            out,err=p.communicate(timeout=4);self.assertEqual(p.returncode,0,err);self.assertIn('admitted',out)
    def test_legacy_global_exclusive_blocks_new_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'runtime').mkdir()
            with (root/'runtime/isaac-gpu.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX);p=self.child(tmp,A)
                with self.assertRaises(subprocess.TimeoutExpired):p.communicate(timeout=.2)
            out,err=p.communicate(timeout=4);self.assertEqual(p.returncode,0,err);self.assertIn('admitted',out)
    def test_new_admission_holds_shared_legacy_barrier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with jobs.worker_gpu_lock(root,A):
                with (root/'runtime/isaac-gpu.lock').open('a') as lock:
                    with self.assertRaises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    def test_cleanup_never_stops_a_different_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=root/'jobs/old';old.mkdir(parents=True)
            identity={'pid':12345,'processToken':'fixture'}
            (old/'worker-process.json').write_text(json.dumps(identity));(old/'worker-request.json').write_text(json.dumps({'device':B}))
            with patch.object(jobs,'stop_process_group') as stop:jobs.stop_previous_gpu_workers(root,root/'jobs/new',A);stop.assert_not_called()
            with patch.object(jobs,'stop_process_group') as stop:jobs.stop_previous_gpu_workers(root,root/'jobs/new',B);stop.assert_called_once_with(identity)

    def test_legacy_cleanup_preserves_a_live_memory_admitted_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root/'jobs/old'
            old.mkdir(parents=True)
            identity = {'pid': 12345, 'processToken': 'fixture'}
            (old/'worker-process.json').write_text(json.dumps(identity))
            (old/'worker-request.json').write_text(json.dumps({'device': A}))
            with patch.object(jobs, '_token', return_value='fixture'), \
                 patch('runtime.gpu_reservation.process_lease', return_value='existing-reservation'), \
                 patch.object(jobs, 'stop_process_group') as stop:
                jobs.stop_previous_gpu_workers(root, root/'jobs/new', A)
                stop.assert_not_called()
    def test_unknown_live_device_fails_without_killing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=root/'jobs/old';old.mkdir(parents=True)
            (old/'worker-process.json').write_text(json.dumps({'pid':12345,'processToken':'fixture'}))
            with patch.object(jobs,'_token',return_value='fixture'),patch.object(jobs,'stop_process_group') as stop:
                with self.assertRaisesRegex(RuntimeError,'unknown GPU'):jobs.stop_previous_gpu_workers(root,root/'jobs/new',A)
                stop.assert_not_called()

    def test_cleanup_does_not_enumerate_historical_job_contents(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=root/'jobs/old';old.mkdir(parents=True)
            (old/'worker-process.json').write_text(json.dumps({'pid':12345,'processToken':'fixture'}))
            (old/'worker-request.json').write_text(json.dumps({'device':A}))
            original=os.scandir
            def guarded(path):
                if Path(path)==old:raise AssertionError('enumerated historical job contents')
                return original(path)
            with patch.object(os,'scandir',side_effect=guarded),patch.object(jobs,'stop_process_group') as stop:
                jobs.stop_previous_gpu_workers(root,root/'jobs/new',A)
                stop.assert_called_once()

    def test_direct_invocation_without_jobs_directory_has_nothing_to_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs.stop_previous_gpu_workers(Path(tmp),Path(tmp)/'invocation',A)
