"""System-only GPU assignment boundary; does not initialize CUDA or run a robot."""
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime import jobs


class ExecutionDeviceTests(unittest.TestCase):
    def test_only_assigned_uuid_is_exposed_and_no_other_card_idle_check_occurs(self):
        device = {'index': 3, 'uuid': 'GPU-30000000-0000-0000-0000-000000000000'}
        environment = {'CUDA_VISIBLE_DEVICES': '0,1,2,3'}
        with patch.object(jobs.subprocess, 'run', return_value=SimpleNamespace(returncode=0,
                stdout='0, GPU-other, 570.211.01\n3, '+device['uuid']+', 570.211.01\n', stderr='')) as query:
            self.assertEqual(jobs.assign_device(device, environment),'570.211.01')
        self.assertEqual(environment['CUDA_VISIBLE_DEVICES'], device['uuid'])
        self.assertEqual(environment['SCENEAGENT_GPU'], '3')
        self.assertEqual(environment['CUDA_DEVICE_ORDER'], 'PCI_BUS_ID')
        self.assertIn('--query-gpu=index,uuid,driver_version', query.call_args.args[0])
        self.assertEqual(query.call_count, 1)

    def test_index_uuid_mismatch_is_rejected_before_sdk_start(self):
        with patch.object(jobs.subprocess, 'run', return_value=SimpleNamespace(returncode=0,
                stdout='3, GPU-other\n', stderr='')):
            with self.assertRaisesRegex(ValueError, 'UUID'):
                jobs.assign_device({'index':3,'uuid':'GPU-30000000-0000-0000-0000-000000000000'}, {})

    def test_missing_or_invalid_device_never_defaults_to_a_card(self):
        for value in (None, {}, {'index':True,'uuid':'GPU-invalid'}, {'index':-1,'uuid':'GPU-invalid'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                jobs.assign_device(value, {})

    def test_native_gpu_three_permission_uses_shared_memory_admission_before_launch(self):
        from runtime import gpu_reservation as gpu
        device = {'index': 3, 'uuid': 'GPU-30000000-0000-0000-0000-000000000000'}
        for permission in (None, False, 1, 'true', True):
            with self.subTest(permission=permission), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                payload = {'root':str(root), 'operation':'observe', 'device':device, 'gpuPeakMiB':7000}
                if permission is not None:
                    payload['allowGpu3'] = permission
                def launch_fixture(*args, **kwargs):
                    with gpu.reserve_gpu(root, device, 7000, wait=False, legacy_barrier=True, allow_gpu3=True):
                        with self.assertRaises(gpu.GpuBusy):
                            with gpu.reserve_gpu(root, device, 7000, wait=False, legacy_barrier=True, allow_gpu3=True):
                                self.fail('unallocated native peaks were not charged')
                    raise OSError('stop before SDK')
                with patch.object(gpu, 'memory_snapshot', return_value=(24564, 20000, {})) as memory, \
                     patch.object(jobs, 'assign_device', return_value='570.211.01'), \
                     patch.object(jobs, 'stop_previous_gpu_workers') as cleanup, \
                     patch.object(jobs.subprocess, 'Popen', side_effect=launch_fixture) as launch:
                    if permission is True:
                        with self.assertRaisesRegex(OSError, 'stop before SDK'):
                            jobs.supervise(payload, lambda *_:[], root/'job')
                        self.assertEqual(memory.call_count, 3)
                        self.assertTrue(all(call.args == (device,) for call in memory.call_args_list))
                        launch.assert_called_once()
                        self.assertIn(gpu.LEASE_ENV, launch.call_args.kwargs['env'])
                        self.assertEqual(list((root/'runtime'/('isaac-'+device['uuid']+'-memory')).glob('*.json')), [])
                    else:
                        with self.assertRaisesRegex(ValueError, 'authorized physical GPU'):
                            jobs.supervise(payload, lambda *_:[], root/'job')
                        memory.assert_not_called()
                        launch.assert_not_called()
                    cleanup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
