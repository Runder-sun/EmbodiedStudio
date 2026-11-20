"""SYSTEM_VERIFICATION_ONLY terminal job reconciliation; no GPU or SDK work."""
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runtime import jobs
from runtime import gpu_reservation as gpu


class TerminalReservationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.job = self.root/'jobs/j1'; self.job.mkdir(parents=True)
        self.device = {'index':2, 'uuid':'GPU-20000000-0000-0000-0000-000000000000'}
        self.lease = 'd68328f97d0a4be8aa09ea1ffb4bc40c'
        self.ledger = self.root/'runtime'/('isaac-'+self.device['uuid']+'-memory'); self.ledger.mkdir(parents=True)
        self.reservation = self.ledger/(self.lease+'.json')
        self.original = json.dumps({'pid':900, 'device':self.device, 'peakMiB':16000}).encode()+b'\n'
        self.reservation.write_bytes(self.original)
        self.other = self.ledger/('a'*32+'.json'); self.other.write_bytes(self.original)
        self.write('worker-request.json', {'device':self.device, 'operation':'rollout'})
        self.write('worker-parent.json', {'pid':900, 'processToken':'parent-token'})
        self.worker = {'pid':901, 'processToken':'worker-token', 'members':{'901':'worker-token','902':'child-token'},
                       'gpuReservationId':self.lease}
        self.write('worker-process.json', self.worker)
        (self.job/'events.jsonl').write_text('')

    def write(self, name, value):
        (self.job/name).write_text(json.dumps(value))

    def terminal(self, state):
        self.write('status.json', {'jobId':'j1', 'state':state, 'pid':900, 'processToken':'parent-token', 'finishedAt':1000})
        self.write('result.json', {'passed':False, 'scope':'original failed task result'})

    def inspect(self, action='status', token=None, members=None, lease=None, present=(), proc_available=True):
        original = Path.stat
        def process_stat(path, *args, **kwargs):
            if path == Path('/proc') or path.parent == Path('/proc') and path.name in ('900', '901', '902'):
                if path == Path('/proc') and proc_available or path.name.isdecimal() and int(path.name) in present:
                    return SimpleNamespace(st_mode=stat.S_IFDIR)
                raise FileNotFoundError('isolated process is absent')
            return original(path, *args, **kwargs)
        with patch.object(jobs, '_token', side_effect=token or (lambda _:None)), \
             patch.object(jobs, '_group_members', side_effect=members or (lambda _:{})), \
             patch.object(jobs, 'stop_process_group', side_effect=AssertionError('terminal reconciliation must never signal')), \
             patch.object(gpu, 'process_lease', return_value=lease), \
             patch.object(Path, 'stat', process_stat), \
             patch.object(gpu.Path, 'glob', return_value=[Path('/proc/901')]):
            return jobs.inspect(self.root, 'j1', action)

    def test_existing_terminal_jobs_archive_only_their_ended_lease_without_changing_results(self):
        for state, action in (('completed','status'), ('failed','wait'), ('cancelled','cancel')):
            with self.subTest(state=state):
                self.terminal(state)
                self.reservation.write_bytes(self.original)
                archived = self.reservation.with_suffix('.released'); archived.unlink(missing_ok=True)
                before = {name:(self.job/name).read_bytes() for name in ('status.json','result.json','worker-process.json','worker-parent.json','worker-request.json')}
                result = self.inspect(action)
                self.assertEqual(result['state'], state)
                self.assertFalse(self.reservation.exists())
                self.assertEqual(archived.read_bytes(), self.original)
                self.assertTrue(self.other.exists())
                for name, data in before.items(): self.assertEqual((self.job/name).read_bytes(), data)
                self.assertEqual(result['result'], json.loads(before['result.json']) if state == 'completed' else None)

    def test_terminal_status_alone_cannot_release_live_or_unknown_processes(self):
        self.terminal('failed')
        for scenario in ('parent', 'worker', 'tracked child', 'worker group', 'parent group', 'lease holder', 'unreadable token'):
            with self.subTest(scenario=scenario):
                def token(pid):
                    if scenario == 'unreadable token': raise PermissionError('process identity unknown')
                    return {900:'parent-token'}.get(pid) if scenario == 'parent' else \
                        {901:'worker-token'}.get(pid) if scenario == 'worker' else \
                        {902:'child-token'}.get(pid) if scenario == 'tracked child' else None
                def members(pid):
                    return {903:'untracked-token'} if (scenario == 'worker group' and pid == 901 or scenario == 'parent group' and pid == 900) else {}
                self.inspect(token=token, members=members, lease=self.lease if scenario == 'lease holder' else None)
                self.assertEqual(self.reservation.read_bytes(), self.original)

    def test_terminal_reconciliation_releases_a_previous_unknown_exit_once_it_is_proven_ended(self):
        self.terminal('failed')
        self.inspect(token=lambda pid:'worker-token' if pid == 901 else None)
        self.assertTrue(self.reservation.exists())
        result = self.inspect()
        self.assertEqual(result['state'], 'failed'); self.assertFalse(self.reservation.exists())
        events = (self.job/'events.jsonl').read_bytes()
        with patch.object(jobs, '_token', side_effect=AssertionError('released lease must not rescan processes')), \
             patch.object(jobs, '_group_members', side_effect=AssertionError('released lease must not rescan groups')):
            jobs.inspect(self.root, 'j1')
        self.assertEqual((self.job/'events.jsonl').read_bytes(), events)

    def test_missing_or_mismatched_native_parent_binding_preserves_the_lease(self):
        self.terminal('cancelled')
        for value in (None, {'pid':999,'processToken':'parent-token'}, {'pid':900,'processToken':'different-parent'}):
            with self.subTest(parent=value):
                if value is None: (self.job/'worker-parent.json').unlink(missing_ok=True)
                else: self.write('worker-parent.json', value)
                self.inspect(); self.assertEqual(self.reservation.read_bytes(), self.original)

    def test_existing_pids_with_unknown_tokens_and_empty_groups_keep_the_reservation(self):
        self.terminal('failed')
        for pid in (900, 901, 902):
            with self.subTest(pid=pid):
                self.inspect(present=(pid,))
                self.assertEqual(self.reservation.read_bytes(), self.original)
                self.assertFalse(self.reservation.with_suffix('.released').exists())

    def test_confirmed_reused_tokens_are_distinct_from_unreadable_process_identity(self):
        self.terminal('failed')
        self.inspect(token=lambda _: 'confirmed-replacement-token', present=(900, 901, 902))
        self.assertFalse(self.reservation.exists())

    def test_unavailable_process_filesystem_or_group_reads_are_not_proof_of_exit(self):
        self.terminal('cancelled')
        self.inspect(proc_available=False)
        self.assertEqual(self.reservation.read_bytes(), self.original)
        def unreadable(_): raise PermissionError('group process state unreadable')
        self.inspect(members=unreadable)
        self.assertEqual(self.reservation.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
