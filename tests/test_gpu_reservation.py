"""CPU fixtures for shared admission; no CUDA allocation."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime import gpu_reservation as gpu
from runtime.jobs import worker_gpu_lock


DEVICE = {'index': 0, 'uuid': 'GPU-00000000-0000-0000-0000-000000000000'}


class SharedGpuTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_same_gpu_can_share_but_unallocated_peaks_are_charged(self):
        with patch.object(gpu, 'memory_snapshot', return_value=(24000, 23000, {})):
            with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False):
                with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False):
                    with self.assertRaises(gpu.GpuBusy):
                        with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False):
                            self.fail('counted the same headroom twice')
                with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False):
                    pass

    def test_only_memory_of_the_matching_lease_is_credited(self):
        with patch.object(gpu, 'memory_snapshot', return_value=(24000, 13500, {101: 4000})):
            with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False) as first:
                with patch.object(gpu, 'process_lease', return_value='unrelated'):
                    with self.assertRaises(gpu.GpuBusy):
                        with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False):
                            pass
                with patch.object(gpu, 'process_lease', return_value=first.id):
                    with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False):
                        pass

    def test_memory_admission_can_run_alongside_a_legacy_holder(self):
        with patch.object(gpu, 'memory_snapshot', return_value=(24000, 23000, {})):
            with worker_gpu_lock(self.root, DEVICE):
                with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False):
                    pass
                with self.assertRaises(gpu.GpuBusy):
                    with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False, legacy_barrier=True):
                        pass
            with self.assertRaises(ValueError):
                with gpu.reserve_gpu(self.root, {**DEVICE, 'index': 3}, 8000, wait=False):
                    pass

    def test_unconfirmed_child_exit_keeps_the_reservation(self):
        with patch.object(gpu, 'memory_snapshot', return_value=(24000, 15000, {})):
            with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False) as lease:
                options = lease.child_options({})
                self.assertEqual(options['env'][gpu.LEASE_ENV], lease.id)
                self.assertNotIn('pass_fds', options)
                with (self.root/'runtime'/('isaac-'+DEVICE['uuid']+'.lock')).open('a') as legacy:
                    fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(gpu.GpuBusy):
                with gpu.reserve_gpu(self.root, DEVICE, 8000, wait=False):
                    pass

    def test_explicit_gpu_three_permission_still_requires_memory_admission(self):
        device = {'index': 3, 'uuid': 'GPU-30000000-0000-0000-0000-000000000000'}
        with patch.object(gpu, 'memory_snapshot', return_value=(24564, 13000, {})):
            with self.assertRaises(gpu.GpuBusy):
                with gpu.reserve_gpu(self.root, device, 12288, wait=False, allow_gpu3=True):
                    pass
            with gpu.reserve_gpu(self.root, device, 11000, wait=False, allow_gpu3=True):
                with self.assertRaises(gpu.GpuBusy):
                    with gpu.reserve_gpu(self.root, device, 2000, wait=False, allow_gpu3=True):
                        pass

    def test_host_policy_enables_new_jobs_without_a_worker_request_change(self):
        self.assertIsNone(gpu.configured_peak(self.root, {}))
        policy = self.root/'runtime/gpu-memory-policy.json'
        policy.parent.mkdir()
        policy.write_text('{"defaultPeakMiB": 16000}')
        self.assertEqual(gpu.configured_peak(self.root, {}), 16000)
        self.assertEqual(gpu.configured_peak(self.root, {'gpuPeakMiB': 7000}), 7000)

    def test_private_worker_roots_share_memory_without_inheriting_legacy_cleanup_domain(self):
        first, second = self.root/'case-a', self.root/'case-b'
        first.mkdir(); second.mkdir()
        with patch.object(gpu, 'memory_snapshot', return_value=(24000, 21000, {})):
            with worker_gpu_lock(self.root, DEVICE):
                with gpu.reserve_gpu(self.root, DEVICE, 7000, wait=False, isolated_root=first):
                    with gpu.reserve_gpu(self.root, DEVICE, 7000, wait=False, isolated_root=second):
                        with self.assertRaises(gpu.GpuBusy):
                            with gpu.reserve_gpu(self.root, DEVICE, 7000, wait=False, isolated_root=first):
                                pass
            with self.assertRaises(ValueError):
                with gpu.reserve_gpu(self.root, DEVICE, 7000, wait=False, isolated_root=self.root):
                    pass


class ReservationReconciliationTests(unittest.TestCase):
    """SYSTEM_VERIFICATION_ONLY isolated ledger and proc records; no GPU jobs."""
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.proc = self.root/'proc'; self.proc.mkdir()
        self.lease = '5cd8fc9df19445c1b47f277a12d1abe8'
        self.ledger = self.root/'runtime'/('isaac-'+DEVICE['uuid']+'-memory'); self.ledger.mkdir(parents=True)
        self.record = self.ledger/(self.lease+'.json')
        self.original = json.dumps({'pid':740636, 'device':DEVICE, 'peakMiB':16000}).encode()+b'\n'
        self.record.write_bytes(self.original)

    def reconcile(self):
        snapshot = gpu._reservation_processes
        with patch.object(gpu, '_reservation_processes', side_effect=lambda *args: snapshot(*args, proc_root=self.proc)):
            return gpu.reconcile_stopped_reservation(self.root, DEVICE, self.lease)

    def process(self, pid, lease=None):
        directory = self.proc/str(pid); directory.mkdir()
        (directory/'environ').write_bytes((gpu.LEASE_ENV+'='+lease).encode()+b'\0' if lease else b'PATH=/usr/bin\0')
        return directory

    def test_confirmed_dead_owner_archives_exact_reservation_and_frees_only_its_peak(self):
        self.process(200)
        other = self.ledger/('a'*32+'.json'); other.write_text(json.dumps({'pid':900, 'device':DEVICE, 'peakMiB':2000}))
        with patch.object(gpu, 'memory_snapshot', return_value=(24564, 22000, {})):
            with self.assertRaises(gpu.GpuBusy):
                with gpu.reserve_gpu(self.root, DEVICE, 7000, wait=False): pass
            result = self.reconcile()
            self.assertEqual(result['status'], 'reconciled')
            self.assertFalse(self.record.exists())
            self.assertEqual(self.record.with_suffix('.reconciled').read_bytes(), self.original)
            proof = json.loads(self.record.with_suffix('.reconciled-proof').read_text())
            self.assertTrue(proof['ownerAbsent']); self.assertEqual(proof['leaseHolders'], [])
            self.assertEqual(proof['ownerUid'], os.geteuid()); self.assertIn(200, proof['inspectedProcessIds'])
            self.assertTrue(other.exists())
            with gpu.reserve_gpu(self.root, DEVICE, 7000, wait=False): pass
        self.assertEqual(self.reconcile()['status'], 'missing')

    def test_live_owner_or_lease_holder_preserves_the_original_reservation(self):
        for owner in (True, False):
            with self.subTest(owner=owner):
                child = self.process(740636 if owner else 201, self.lease)
                result = self.reconcile()
                self.assertEqual(result['status'], 'preserved')
                self.assertEqual(self.record.read_bytes(), self.original)
                self.assertFalse(self.record.with_suffix('.reconciled').exists())
                (child/'environ').unlink(); child.rmdir()

    def test_unreadable_or_incomplete_same_owner_process_state_is_preserved(self):
        child = self.process(201)
        original = Path.read_bytes
        def unreadable(path):
            if path == child/'environ': raise PermissionError('actual proc read denied')
            return original(path)
        with patch.object(Path, 'read_bytes', unreadable):
            self.assertEqual(self.reconcile()['status'], 'preserved')
        (child/'environ').unlink()
        self.assertEqual(self.reconcile()['status'], 'preserved')
        self.assertEqual(self.record.read_bytes(), self.original)

    def test_owner_reappearing_during_process_scan_is_preserved(self):
        child = self.process(201); original = Path.read_bytes
        def reappeared(path):
            if path == child/'environ': (self.proc/'740636').mkdir(exist_ok=True)
            return original(path)
        with patch.object(Path, 'read_bytes', reappeared):
            self.assertEqual(self.reconcile()['status'], 'preserved')
        self.assertEqual(self.record.read_bytes(), self.original)

    def test_wrong_device_or_missing_process_filesystem_cannot_free_a_reservation(self):
        with self.assertRaises(ValueError): gpu.reconcile_stopped_reservation(self.root, {**DEVICE, 'index':2}, self.lease)
        self.proc.rmdir()
        self.assertEqual(self.reconcile()['status'], 'preserved')
        self.assertEqual(self.record.read_bytes(), self.original)

    def test_failed_proof_write_keeps_the_reservation_charged(self):
        original = Path.open
        def failed(path, *args, **kwargs):
            if path.name.endswith('.reconciled-proof'): raise OSError('proof storage failed')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', failed), self.assertRaisesRegex(OSError, 'proof storage failed'):
            self.reconcile()
        self.assertEqual(self.record.read_bytes(), self.original)
        self.assertFalse(self.record.with_suffix('.reconciled').exists())
