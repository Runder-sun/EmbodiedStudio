"""Memory admission for cooperating workers on the compute host. No GPU allocation here."""
import argparse
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET


LEASE_ENV = 'SCENEAGENT_GPU_MEMORY_LEASE'
HEADROOM_MIB = 1024


class GpuBusy(RuntimeError):
    pass


def configured_peak(root, payload):
    if 'gpuPeakMiB' in payload:
        return payload['gpuPeakMiB']
    policy = Path(root)/'runtime/gpu-memory-policy.json'
    if policy.is_symlink():
        raise ValueError('GPU admission policy must be a plain file')
    if not policy.exists():
        return None
    value = json.loads(policy.read_text())
    if set(value) != {'defaultPeakMiB'} or type(value['defaultPeakMiB']) is not int or value['defaultPeakMiB'] <= 0:
        raise ValueError('invalid GPU memory admission policy')
    return value['defaultPeakMiB']


def memory_snapshot(device):
    result = subprocess.run(['nvidia-smi', '-q', '-x', '-i', str(device['index'])],
                            capture_output=True, text=True, check=True, timeout=10)
    cards = ET.fromstring(result.stdout).findall('gpu')
    if len(cards) != 1 or cards[0].findtext('uuid') != device['uuid']:
        raise ValueError('assigned GPU index does not match its actual UUID')
    card = cards[0]

    def mib(value):
        match = re.fullmatch(r'(\d+) MiB', value or '')
        return int(match[1]) if match else None

    total = mib(card.findtext('fb_memory_usage/total'))
    free = mib(card.findtext('fb_memory_usage/free'))
    if total is None or free is None or not 0 <= free <= total:
        raise ValueError('unavailable GPU memory measurement')
    used = {}
    for app in card.findall('processes/process_info'):
        pid, memory = app.findtext('pid', ''), mib(app.findtext('used_memory'))
        if pid.isdecimal() and memory is not None:
            used[int(pid)] = max(used.get(int(pid), 0), memory)
    return total, free, used


def process_lease(pid):
    proc = Path('/proc')/str(pid)
    try:
        if proc.stat().st_uid != os.geteuid():
            return None
        marker = LEASE_ENV.encode()+b'='
        return next((v[len(marker):].decode('ascii') for v in (proc/'environ').read_bytes().split(b'\0')
                     if v.startswith(marker)), None)
    except (OSError, UnicodeError):
        return None


@dataclass
class Reservation:
    id: str
    fds: tuple = ()
    dispatched: bool = False
    finished: bool = False

    def child_options(self, environment):
        self.dispatched = True
        options = {'env': {**environment, LEASE_ENV: self.id}}
        if self.fds:
            options['pass_fds'] = self.fds
        return options


def _unfulfilled(ledger, used):
    owned = {}
    for pid, memory in used.items():
        if owner := process_lease(pid):
            owned[owner] = owned.get(owner, 0)+memory
    return sum(max(0, json.loads(file.read_text())['peakMiB']-owned.get(file.stem, 0))
               for file in ledger.glob('*.json'))


def release_stopped_reservation(root, device, lease_id, supervisor_pid):
    """Release only the recorded lease after the job's process groups have stopped."""
    if not re.fullmatch(r'[0-9a-f]{32}', lease_id):
        raise ValueError('invalid stopped GPU reservation identity')
    uuid_value = (device or {}).get('uuid', '')
    if not re.fullmatch(r'GPU-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', uuid_value):
        raise ValueError('invalid stopped GPU reservation device')
    ledger = Path(root) / 'runtime' / ('isaac-' + uuid_value + '-memory')
    record = ledger / (lease_id + '.json')
    if not ledger.is_dir():
        return
    with (ledger / 'admission.lock').open('a') as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        if not record.is_file():
            return
        row = json.loads(record.read_text())
        if row['pid'] != supervisor_pid or row['device'] != device:
            raise ValueError('stopped GPU reservation owner differs from the job')
        if any(process_lease(proc.name) == lease_id for proc in Path('/proc').glob('[0-9]*')):
            return
        archived = record.with_suffix('.released')
        if archived.exists() or archived.is_symlink():
            return False
        record.rename(archived)
        return True


def _reservation_processes(supervisor_pid, lease_id, owner_uid, *, proc_root=Path('/proc')):
    """Native lease descendants keep the supervisor UID and lease environment."""
    if not proc_root.is_dir():
        raise ValueError('native process filesystem is unavailable')
    state = {'ownerAbsent': False, 'ownerUid': owner_uid, 'leaseHolders': [], 'inspectedProcessIds': []}
    try:
        (proc_root/str(supervisor_pid)).stat()
        return state
    except FileNotFoundError:
        pass
    marker = (LEASE_ENV+'='+lease_id).encode()
    for proc in proc_root.iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if proc.stat().st_uid != owner_uid:
                continue
            environment = (proc/'environ').read_bytes()
        except FileNotFoundError:
            # An unreadable live process is unknown, not proof of no lease.
            if proc.exists():
                raise ValueError('live native process environment is unavailable')
            continue
        state['inspectedProcessIds'].append(int(proc.name))
        if marker in environment.split(b'\0'):
            state['leaseHolders'].append(int(proc.name))
    try:
        (proc_root/str(supervisor_pid)).stat()
    except FileNotFoundError:
        state['ownerAbsent'] = True
    return state


def reconcile_stopped_reservation(root, device, lease_id):
    """Explicitly archive one proven orphan; uncertain reservations stay charged."""
    if (not re.fullmatch(r'[0-9a-f]{32}', lease_id or '') or not isinstance(device, dict)
            or set(device) != {'index', 'uuid'} or type(device['index']) is not int or device['index'] < 0
            or not re.fullmatch(r'GPU-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', device.get('uuid', ''))):
        raise ValueError('invalid GPU reservation reconciliation identity')
    ledger = Path(root)/'runtime'/('isaac-'+device['uuid']+'-memory')
    record = ledger/(lease_id+'.json')
    if ledger.is_symlink() or record.is_symlink() or (ledger/'admission.lock').is_symlink():
        raise ValueError('GPU reconciliation requires plain ledger paths')
    if not ledger.is_dir():
        return {'status': 'missing', 'leaseId': lease_id}
    with (ledger/'admission.lock').open('a') as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        if not record.is_file():
            return {'status': 'missing', 'leaseId': lease_id}
        original = record.read_bytes(); row = json.loads(original)
        if (row.get('device') != device or type(row.get('pid')) is not int or row['pid'] <= 0
                or type(row.get('peakMiB')) is not int or row['peakMiB'] <= 0):
            raise ValueError('GPU reconciliation reservation owner or device differs')
        owner_uid = record.stat().st_uid
        if owner_uid != os.geteuid():
            return {'status': 'preserved', 'leaseId': lease_id, 'reason': 'reservation belongs to another user'}
        try:
            state = _reservation_processes(row['pid'], lease_id, owner_uid)
        except (OSError, ValueError) as error:
            return {'status': 'preserved', 'leaseId': lease_id, 'reason': str(error)}
        if not state['ownerAbsent'] or state['leaseHolders']:
            return {'status': 'preserved', 'leaseId': lease_id, **state}
        archived, proof = record.with_suffix('.reconciled'), record.with_suffix('.reconciled-proof')
        if archived.exists() or proof.exists():
            return {'status': 'preserved', 'leaseId': lease_id, 'reason': 'prior reconciliation evidence requires inspection'}
        evidence = {'leaseId': lease_id, 'reservation': row, 'reservationSha256': hashlib.sha256(original).hexdigest(),
                    'checkedAtUnixMs': int(time.time()*1000), **state}
        with proof.open('x') as output:
            output.write(json.dumps(evidence)+'\n')
            output.flush(); os.fsync(output.fileno())
        record.rename(archived)
        return {'status': 'reconciled', 'leaseId': lease_id, 'archived': str(archived), 'proof': str(proof), **state}


@contextmanager
def reserve_gpu(root, device, peak_mib, *, wait=True, legacy_barrier=False, isolated_root=None, allow_gpu3=False):
    allowed_indices = (0, 1, 2, 3) if allow_gpu3 else (0, 1, 2)
    if (not isinstance(device, dict) or set(device) != {'index', 'uuid'}
            or type(device['index']) is not int or device['index'] not in allowed_indices
            or not isinstance(device['uuid'], str)
            or not re.fullmatch(r'GPU-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', device['uuid'])):
        raise ValueError('memory admission requires an authorized physical GPU index and its UUID')
    if type(peak_mib) is not int or peak_mib <= 0:
        raise ValueError('GPU peak reservation must be a positive integer MiB')
    root = Path(root).resolve()
    runtime = root/'runtime'
    name = 'isaac-'+device['uuid']
    ledger = runtime/(name+'-memory')
    ledger.mkdir(parents=True, exist_ok=True)
    lock_paths, mode = (), fcntl.LOCK_SH
    lock_reason = 'legacy GPU worker still holds the exclusive lock'
    if legacy_barrier:
        lock_paths = (runtime/'isaac-gpu.lock', runtime/(name+'.lock'))
    if isolated_root is not None:
        private = Path(isolated_root).resolve()
        if legacy_barrier or private == root or not private.is_relative_to(root) or not private.is_dir():
            raise ValueError('use a distinct private worker root inside the host data root')
        # A frozen evaluator only cleans jobs inside its own worker root.
        private_runtime = private/'runtime'
        private_runtime.mkdir(exist_ok=True)
        lock_paths = (private_runtime/'external-memory.lock',)
        mode, lock_reason = fcntl.LOCK_EX, 'another reservation is using this private worker root'
    while True:
        with ExitStack() as held:
            try:
                locks = [held.enter_context(path.open('a')) for path in lock_paths]
                for lock in locks:
                    fcntl.flock(lock, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                reason = lock_reason
            else:
                with (ledger/'admission.lock').open('a') as gate:
                    fcntl.flock(gate, fcntl.LOCK_EX)
                    total, free, used = memory_snapshot(device)
                    if peak_mib+HEADROOM_MIB > total:
                        raise ValueError('GPU reservation exceeds the device capacity')
                    pending = _unfulfilled(ledger, used)
                    reason = f'{free} MiB free, {pending} MiB already reserved, {peak_mib} MiB requested'
                    if free >= pending+peak_mib+HEADROOM_MIB:
                        reservation = Reservation(uuid.uuid4().hex, tuple(lock.fileno() for lock in locks))
                        record = ledger/(reservation.id+'.json')
                        staged = record.with_suffix('.tmp')
                        staged.write_text(json.dumps({'pid': os.getpid(), 'device': device, 'peakMiB': peak_mib})+'\n')
                        staged.replace(record)
                        held = held.pop_all()
                        break
        if not wait:
            raise GpuBusy(reason)
        time.sleep(2)

    try:
        yield reservation
    finally:
        try:
            # An unconfirmed exit keeps its full reservation for operator inspection.
            if not reservation.dispatched or reservation.finished:
                with (ledger/'admission.lock').open('a') as gate:
                    fcntl.flock(gate, fcntl.LOCK_EX)
                    record.unlink()
        finally:
            held.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--gpu-index', type=int, required=True)
    parser.add_argument('--gpu-uuid', required=True)
    parser.add_argument('--peak-mib', type=int, required=True)
    parser.add_argument('--isolated-worker-root')
    parser.add_argument('--allow-gpu3', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('provide a worker command after --')
    with reserve_gpu(args.root, {'index': args.gpu_index, 'uuid': args.gpu_uuid}, args.peak_mib,
                     isolated_root=args.isolated_worker_root, allow_gpu3=args.allow_gpu3) as lease:
        print(f'GPU {args.gpu_index}: reserved {args.peak_mib} MiB (lease {lease.id})', file=sys.stderr, flush=True)
        try:
            environment = {**os.environ, 'CUDA_VISIBLE_DEVICES': args.gpu_uuid,
                           'CUDA_DEVICE_ORDER': 'PCI_BUS_ID', 'SCENEAGENT_GPU': str(args.gpu_index)}
            child = subprocess.Popen(command, **lease.child_options(environment))
        except OSError:
            lease.finished = True
            raise
        code = child.wait()
        # Legacy frozen evaluators normally reap their Kit worker before returning.
        # Keep charging an orphan that still carries the identity of this launch.
        while any(process_lease(pid.name) == lease.id for pid in Path('/proc').iterdir() if pid.name.isdecimal()):
            time.sleep(1)
        lease.finished = True
        return code if code >= 0 else 128-code


if __name__ == '__main__':
    raise SystemExit(main())
