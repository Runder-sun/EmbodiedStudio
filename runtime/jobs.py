"""Durable remote processes: one request, a process identity, ordered receipts."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from runtime.records import atomic_json
from runtime.scene import valid_id


# Child counters stay in memory; only existing progress/business boundaries persist them.
_timing = None
_parent_timing = None


def worker_timeout(payload):
    value = payload.get('workerTimeoutSeconds', 900)
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('worker timeout must be finite positive seconds')
    return value


@contextmanager
def measure(name, parent=None):
    counters = parent if parent is not None else (_timing['document']['groups'] if _timing is not None else None)
    if counters is None:
        yield
        return
    started, failed = time.perf_counter(), False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        row = counters.setdefault(name, {'seconds': 0., 'count': 0, 'failedCount': 0})
        row['seconds'] = row.get('seconds', 0.) + time.perf_counter() - started
        row['count'] = row.get('count', 0) + 1
        row['failedCount'] = row.get('failedCount', 0) + int(failed)
        if parent is not None:
            row['state'] = 'failed' if failed else 'completed'


def _diagnostic_error(error):
    try:
        print('performance diagnostic incomplete: ' + str(error), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def start_performance(directory, payload):
    global _timing
    document = {'schema': 'sceneagent-performance-v1', 'requestHash': payload_digest(payload),
        'operation': payload['operation'], 'runtimeSnapshot': str(Path(__file__).resolve().parent),
        'workerTimeoutSeconds': worker_timeout(payload),
        'worker': {'pid': os.getpid(), 'processToken': _token(os.getpid())},
        'phase': 'initialized', 'groups': {}, 'inclusive': True, 'incomplete': True, 'diagnosticFailed': False,
        'unflushedTail': 'unknown', 'flushMeasurement': 'previous_completed_flushes',
        'shutdown': {'state': 'unknown'}}
    for name in ('projectId', 'revision'):
        if payload.get(name) is not None:
            document[name] = payload[name]
    _timing = {'directory': Path(directory), 'document': document}


@contextmanager
def performance_context(directory, payload):
    global _timing
    previous = _timing
    start_performance(directory, payload)
    try:
        yield
    finally:
        _timing = previous


def _phase_progress(value):
    if not isinstance(value, dict) or set(value)-{'taskId', 'episodeId', 'completed', 'total'}:
        raise ValueError('invalid diagnostic progress')
    for name in ('taskId', 'episodeId'):
        if name in value and (not isinstance(value[name], str) or not 0 < len(value[name]) <= 80):
            raise ValueError('invalid diagnostic progress identity')
    for name in ('completed', 'total'):
        if name in value and (type(value[name]) is not int or value[name] < 0):
            raise ValueError('invalid diagnostic progress count')
        if name in value:
            _performance_number(value[name])
    if 'completed' in value and 'total' in value and value['completed'] > value['total']:
        raise ValueError('invalid diagnostic progress boundary')
    return dict(value)


def flush_performance(phase, result=None, *, progress=None):
    if _timing is None:
        return
    document = _timing['document']
    document.update(phase=phase, lastSnapshotAt=time.time(), lastSnapshotCounter=time.perf_counter())
    if result:
        for name in ('projectId', 'revision'):
            if result.get(name) is not None:
                document[name] = result[name]
    try:
        document.pop('progress', None)
        if progress is not None:
            document['progress'] = _phase_progress(progress)
        with measure('diagnosticFlush'):
            atomic_json(_timing['directory'] / 'performance.json', document)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
        document['incomplete'] = True
        document['diagnosticFailed'] = True
        _diagnostic_error(error)


def _timeout_phase(directory, payload, identity):
    """A bounded diagnostic prefix is useful only for this exact live worker/request."""
    try:
        with (Path(directory)/'performance.json').open('rb') as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError('performance prefix too large')
        document = json.loads(raw, parse_constant=_nonfinite_performance)
        if (not isinstance(document, dict) or not identity.get('processToken') or _token(identity['pid']) != identity['processToken']
                or document.get('schema') != 'sceneagent-performance-v1'
                or document.get('requestHash') != payload_digest(payload)
                or document.get('operation') != payload['operation']
                or document.get('worker') != {name: identity[name] for name in ('pid', 'processToken')}):
            raise ValueError('performance request or worker identity mismatch')
        phase = document.get('phase')
        if not isinstance(phase, str) or not 0 < len(phase) <= 160:
            raise ValueError('invalid diagnostic phase')
        snapshot = {'phase': phase, 'worker': document['worker']}
        for name in ('lastSnapshotAt', 'lastSnapshotCounter'):
            if name in document:
                snapshot[name] = _performance_number(document[name])
        if 'progress' in document:
            snapshot['progress'] = _phase_progress(document['progress'])
        return 'last worker phase: '+json.dumps(snapshot)[:1600]
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError) as error:
        return 'last worker phase unavailable: '+str(error)[:1000]


def shutdown_started(application_started):
    if _timing is not None:
        _timing['document']['shutdown'] = ({'state': 'started', 'count': 1, 'startedCounter': time.perf_counter()}
                                         if application_started else {'state': 'not_applicable'})
        flush_performance('shutdown_started')


def _parent_performance(directory, payload, scope):
    return {'directory': Path(directory), 'payload': payload, 'scope': scope,
            'counters': {'publication': {'state': 'not_executed' if scope == 'job' else 'not_applicable'}}}


def _nonfinite_performance(value):
    raise ValueError('nonfinite performance value: ' + value)



def _performance_number(value):
    if type(value) not in (int, float):
        raise ValueError('performance number has invalid type')
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError('performance number exceeds finite float range') from error
    if not math.isfinite(number) or number < 0:
        raise ValueError('performance number must be finite and nonnegative')
    return number


def _performance_counter(row):
    required = {'seconds', 'count', 'failedCount'}
    if not isinstance(row, dict) or not required <= set(row) <= required | {'state', 'measurement'}:
        raise ValueError('invalid performance counter shape')
    _performance_number(row['seconds'])
    if (type(row['count']) is not int or type(row['failedCount']) is not int
            or not 0 <= row['failedCount'] <= row['count']):
        raise ValueError('invalid performance counter counts')
    _performance_number(row['count'])
    _performance_number(row['failedCount'])
    if 'state' in row and row['state'] not in ('completed', 'failed'):
        raise ValueError('invalid performance counter state')
    if 'measurement' in row and row['measurement'] != 'observed_upper_bound':
        raise ValueError('invalid performance counter measurement')
    return dict(row)


def _performance_shutdown(shutdown):
    if not isinstance(shutdown, dict):
        raise ValueError('invalid performance shutdown prefix')
    state = shutdown.get('state')
    if state in ('unknown', 'not_applicable') and set(shutdown) == {'state'}:
        return dict(shutdown)
    required = {'state', 'startedCounter'}
    if state == 'observed_exit':
        required |= {'seconds', 'measurement', 'includesSupervisorPollDelay'}
        if shutdown.get('measurement') != 'observed_upper_bound' or shutdown.get('includesSupervisorPollDelay') is not True:
            raise ValueError('invalid performance shutdown measurement')
        _performance_number(shutdown.get('seconds'))
    elif state != 'started':
        raise ValueError('invalid performance shutdown state')
    if not required <= set(shutdown) <= required | {'count'}:
        raise ValueError('invalid performance shutdown fields')
    if 'count' in shutdown and (type(shutdown['count']) is not int or shutdown['count'] != 1):
        raise ValueError('invalid performance shutdown count')
    return {**shutdown, 'startedCounter': _performance_number(shutdown['startedCounter'])}


def _performance_parent_fields(prior):
    timings = ('gpuLockWait', 'supervision', 'verification', 'publication', 'workerWall', 'diagnosticFlush')
    if not isinstance(prior, dict) or set(prior) - set(timings) - {'workerExitCode', 'observedExitCounter', 'cleanupReturnCode', 'cleanupCompletedCounter', 'diagnosticIncomplete', 'workerTimeoutSeconds'}:
        raise ValueError('invalid performance parent fields')
    fields = {}
    for name, value in prior.items():
        if name in timings:
            if name == 'publication' and isinstance(value, dict) and value.get('state') in ('not_executed', 'not_applicable') and set(value) == {'state'}:
                fields[name] = dict(value)
            else:
                fields[name] = _performance_counter(value)
        elif name in ('workerExitCode', 'cleanupReturnCode'):
            if type(value) is not int:
                raise ValueError('invalid performance worker exit code')
            _performance_number(abs(value))
            fields[name] = value
        elif name == 'workerTimeoutSeconds':
            fields[name] = worker_timeout({name: value})
        elif name in ('observedExitCounter', 'cleanupCompletedCounter'):
            fields[name] = _performance_number(value)
        else:
            if type(value) is not bool:
                raise ValueError('invalid performance diagnostic flag')
            fields[name] = value
    return fields


def _write_performance(directory, parent=None, status=None, identity=None, exit_code=None, observed_exit=None,
                       cleanup_return_code=None, cleanup_completed=None):
    """CPU parent owns the terminal lock; natural and cleanup exit facts stay distinct."""
    directory = Path(directory)
    request_hash, operation = None, None
    try:
        if parent is None:
            payload = json.loads((directory / 'worker-request.json').read_text())
            parent = _parent_performance(directory, payload, 'job' if status else 'worker_verification_only')
        payload = parent['payload']
        request_hash, operation = payload_digest(payload), payload['operation']
        if identity is None:
            identity = json.loads((directory / 'worker-process.json').read_text())
        with (directory / 'performance.json').open('rb') as file:
            data = file.read(262145)
        if len(data) > 262144:
            raise ValueError('performance prefix too large')
        document = json.loads(data, parse_constant=_nonfinite_performance)
        if (not isinstance(document, dict) or document.get('schema') != 'sceneagent-performance-v1'
                or document.get('requestHash') != payload_digest(payload)
                or document.get('operation') != payload['operation']
                or document.get('worker') != {'pid': identity['pid'], 'processToken': identity['processToken']}):
            raise ValueError('performance request or worker identity mismatch')
        if 'workerTimeoutSeconds' in document and worker_timeout(document) != worker_timeout(payload):
            raise ValueError('performance worker timeout differs from the actual request')
        groups = document.get('groups')
        if not isinstance(groups, dict) or any(not isinstance(name, str) or not 0 < len(name) <= 128
                or not name.strip() or not name.isprintable() for name in groups):
            raise ValueError('invalid performance groups')
        document['groups'] = {name: _performance_counter(row) for name, row in groups.items()}
        document['shutdown'] = _performance_shutdown(document.get('shutdown'))
        prior = _performance_parent_fields(document['parent']) if 'parent' in document else {}
        if 'workerTimeoutSeconds' in prior and prior['workerTimeoutSeconds'] != worker_timeout(payload):
            raise ValueError('performance parent timeout differs from the actual request')
        # Only validated fixed parent fields may survive recovery.
        if status and parent['counters']['publication']['state'] == 'not_executed' and document.get('terminalState') is None:
            if any(name in prior for name in ('workerExitCode', 'cleanupReturnCode', 'cleanupCompletedCounter')):
                parent['counters'] = {**prior, **parent['counters']}
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError) as error:
        _diagnostic_error(error)
        document = {'schema': 'sceneagent-performance-v1', 'groups': {}, 'inclusive': True,
                    'incomplete': True, 'diagnosticFailed': True, 'diagnosticError': str(error)[:1000], 'unflushedTail': 'unknown',
                    'shutdown': {'state': 'unknown'}}
        if parent is None:
            parent = {'scope': 'job' if status else 'worker_verification_only',
                      'counters': {'publication': {'state': 'not_executed' if status else 'not_applicable'}}}
        if request_hash is not None:
            document.update(requestHash=request_hash, operation=operation)
    try:
        if request_hash is not None:
            document['workerTimeoutSeconds'] = worker_timeout(payload)
            parent['counters']['workerTimeoutSeconds'] = document['workerTimeoutSeconds']
        document.update(scope=parent['scope'], parent=parent['counters'], parentSnapshotAt=time.time())
        if parent.get('runtimeSnapshot') is not None:
            document['runtimeSnapshot'] = parent['runtimeSnapshot']
        if cleanup_return_code is not None:
            if type(cleanup_return_code) is not int:
                raise ValueError('invalid performance cleanup exit code')
            _performance_number(abs(cleanup_return_code))
            document['parent']['cleanupReturnCode'] = cleanup_return_code
        if cleanup_completed is not None:
            document['parent']['cleanupCompletedCounter'] = _performance_number(cleanup_completed)
        if observed_exit is not None:
            observed_exit = _performance_number(observed_exit)
            document['parent'].update(workerExitCode=exit_code, observedExitCounter=observed_exit)
            shutdown = document['shutdown']
            if shutdown.get('state') == 'started' and type(shutdown.get('startedCounter')) in (int, float):
                seconds = observed_exit - shutdown['startedCounter']
                if 0 <= seconds < float('inf'):
                    document['shutdown'] = {**shutdown, 'state': 'observed_exit', 'seconds': seconds,
                        'measurement': 'observed_upper_bound', 'includesSupervisorPollDelay': True}
                else:
                    document['incomplete'] = True
            elif shutdown.get('state') != 'not_applicable':
                document['incomplete'] = True
        counters = parent['counters']
        verification = counters.get('verification')
        verified = (counters.get('workerExitCode') == 0 and isinstance(verification, dict) and verification.get('state') == 'completed'
                    and document.get('phase') == 'shutdown_started' and document['shutdown'].get('state') in ('observed_exit', 'not_applicable'))
        complete = verified and (parent['scope'] == 'worker_verification_only' or status is not None
                                and status['state'] == 'completed' and counters['publication']['state'] == 'completed')
        document['incomplete'] = bool(not complete or document.get('diagnosticFailed') or counters.get('diagnosticIncomplete'))
        if status:
            document['terminalState'] = status['state']
        with measure('diagnosticFlush', parent['counters']):
            atomic_json(directory / 'performance.json', document)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
        parent['counters']['diagnosticIncomplete'] = True
        _diagnostic_error(error)


def _token(pid):
    path = Path(f'/proc/{pid}/stat')
    if path.exists():
        try:
            fields = path.read_text().split(') ', 1)[1].split()
        except FileNotFoundError:
            return None
        if fields[0] == 'Z':
            return None
        return fields[19]
    try:
        os.kill(pid, 0)
        return 'portable-process'
    except ProcessLookupError:
        return None


@contextmanager
def terminal_lock(directory):
    import fcntl
    with (Path(directory) / 'terminal.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _group_members(pgid):
    members = {}
    for file in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = file.read_text().split(') ', 1)[1].split()
            if fields[0] != 'Z' and int(fields[2]) == pgid:
                members[int(file.parent.name)] = fields[19]
        except (FileNotFoundError, ProcessLookupError):
            continue
    return members


def stop_process_group(identity, process=None):
    pid, token = identity.get('pid'), identity.get('processToken')
    if not pid:
        return
    tracked = {int(key): value for key, value in identity.get('members', {}).items()}
    if _token(pid) == token:
        tracked.update(_group_members(pid))
        tracked[pid] = token
    def alive():
        if process is not None and process.poll() is not None:
            tracked.pop(pid, None)
        return any(_token(member) == expected for member, expected in tracked.items())
    if not alive():
        return
    for command, duration in ((signal.SIGTERM, 2), (signal.SIGKILL, 2)):
        try:
            os.killpg(pid, command)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + duration
        while alive() and time.monotonic() < deadline:
            time.sleep(.025)
        if not alive():
            return
    raise RuntimeError('process group termination not confirmed')


def _stop_worker(directory):
    identity = Path(directory) / 'worker-process.json'
    if identity.exists():
        worker = json.loads(identity.read_text())
        stop_process_group(worker)
        return worker


def _release_stopped_reservation(root, directory, supervisor, worker):
    from runtime.gpu_reservation import release_stopped_reservation

    lease_id = worker.get('gpuReservationId') if isinstance(worker, dict) else None
    if isinstance(lease_id, str) and lease_id:
        payload = json.loads((directory / 'worker-request.json').read_text())
        return release_stopped_reservation(root, payload.get('device'), lease_id, supervisor['pid'])


def _reconcile_terminal_reservation(root, directory, status):
    """A terminal result alone never authorizes lease release or process signals."""
    try:
        if (status.get('state') not in ('completed', 'failed', 'cancelled') or type(status.get('pid')) is not int
                or status['pid'] <= 0 or not isinstance(status.get('processToken'), str) or not status['processToken']):
            return False
        worker = json.loads((directory/'worker-process.json').read_text())
        if not isinstance(worker.get('gpuReservationId'), str) or not worker['gpuReservationId']:
            return False
        payload = json.loads((directory/'worker-request.json').read_text())
        reservation = Path(root)/'runtime'/('isaac-'+valid_id(payload['device']['uuid'])+'-memory')/(valid_id(worker['gpuReservationId'])+'.json')
        if not reservation.is_file():
            return False
        parent = json.loads((directory/'worker-parent.json').read_text())
        if parent.get('pid') != status['pid'] or parent.get('processToken') != status['processToken']:
            return False
        def ended(pid, expected):
            current = _token(pid)
            if current is not None:
                return isinstance(current, str) and bool(current) and current not in (expected, 'portable-process')
            try:
                Path('/proc/'+str(pid)).stat()
            except FileNotFoundError:
                return Path('/proc').is_dir()
            return False
        for identity in (status, worker):
            pid, token = identity.get('pid'), identity.get('processToken')
            if type(pid) is not int or pid <= 0 or not isinstance(token, str) or not token:
                return False
            if not ended(pid, token) or _group_members(pid):
                return False
            members = identity.get('members', {})
            if not isinstance(members, dict) or any(not isinstance(member, str) or not member.isdecimal() or int(member) <= 0
                    or not isinstance(value, str) or not value for member, value in members.items()):
                return False
            if any(not ended(int(member), expected) for member, expected in members.items()):
                return False
        if _release_stopped_reservation(root, directory, status, worker):
            append_event(directory, {'type':'gpu_reservation_released', 'gpuReservationId':worker['gpuReservationId'],
                'supervisor':{'pid':status['pid'], 'processToken':status['processToken']}, 'worker':worker,
                'processGroupsConfirmedStopped':[status['pid'], worker['pid']]})
            return True
    except (OSError, ValueError, KeyError, TypeError):
        # Missing identity/permission evidence keeps the original reservation charged.
        return False
    return False


def _terminal(directory, state, result=None, error=None, finalize=None, performance=None):
    with terminal_lock(directory):
        status = json.loads((directory / 'status.json').read_text())
        if status['state'] not in ('queued', 'running'):
            return status
        if state == 'completed':
            if finalize:
                with measure('publication', performance['counters'] if performance else None):
                    result = finalize(result)
            atomic_json(directory / 'result.json', result)
            event = {'type': 'result', 'result': result}
        else:
            (directory / 'result.json').unlink(missing_ok=True)
            event = {'type': 'cancelled' if state == 'cancelled' else 'error',
                     'jobId': status['jobId'], 'error': error}
        status.update(state=state, finishedAt=time.time())
        if error:
            status['error'] = error
        atomic_json(directory / 'status.json', status)
        append_event(directory, event)
        if performance is not None:
            _write_performance(directory, performance, status)
        return status


def payload_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def file_digest(path):
    content = Path(path).read_bytes()
    return {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}


def write_candidate(directory, payload, result, files, barriers, termination=None):
    root = Path(payload['root']).resolve()
    atomic_json(Path(directory) / 'candidate.json', {
        'schema': 'sceneagent-worker-candidate-v3', 'engine': 'isaacsim', 'physics': 'physx', 'operation': payload['operation'],
        'requestHash': payload_digest(payload), 'businessExitCode': 0,
        'workerTimeoutSeconds': worker_timeout(payload),
        'terminationMode': (termination or {}).get('terminationMode', 'official_fast_shutdown'),
        'fullVendorCleanupVerified': False,
        'barriers': barriers, 'result': result,
        'files': {str(Path(path).resolve().relative_to(root)): file_digest(path) for path in files}})


def verify_candidate(directory, payload, exit_code, required_files):
    if exit_code != 0:
        detail = ''
        try:
            with (Path(directory) / 'worker-failure.json').open('rb') as file:
                data = file.read(262145)
            if len(data) <= 262144:
                failure = json.loads(data)
                error = failure.get('error') if isinstance(failure, dict) else None
                if isinstance(error, str) and error:
                    detail = ': ' + error[:2000]
        except (OSError, ValueError):
            pass
        log_path = Path(directory) / 'kit.log'
        try:
            with log_path.open('rb') as file:
                file.seek(0, os.SEEK_END)
                offset = max(0, file.tell() - 262144)
                file.seek(offset)
                if offset:
                    file.readline()
                lines = file.read(262144).decode('utf8', errors='replace').splitlines()
            evidence = [line[:500] for line in lines if '[Error] [omni.rtx]' in line
                        and ('Out of GPU memory allocating resource ' in line or 'VkResult: ERROR_OUT_OF_DEVICE_MEMORY' in line)][:2]
            if evidence:
                detail += '; 日志记录 RTX 显存分配失败（' + str(log_path) + '）: ' + ' | '.join(evidence)
        except OSError:
            pass
        raise RuntimeError(f'Isaac worker exit is not success: {exit_code}' + detail)
    candidate = json.loads((Path(directory) / 'candidate.json').read_text())
    if candidate.get('schema') != 'sceneagent-worker-candidate-v3' or candidate.get('requestHash') != payload_digest(payload):
        raise ValueError('worker candidate request identity mismatch')
    if (worker_timeout({'workerTimeoutSeconds': candidate.get('workerTimeoutSeconds', 900)}) != worker_timeout(payload)
            or 'workerTimeoutSeconds' in payload and 'workerTimeoutSeconds' not in candidate):
        raise ValueError('worker candidate timeout budget differs from the request')
    if candidate.get('engine') != 'isaacsim' or candidate.get('physics') != 'physx':
        raise ValueError('worker engine protocol mismatch')
    if candidate.get('operation') != payload['operation'] or type(candidate.get('businessExitCode')) is not int or candidate['businessExitCode'] != 0:
        raise ValueError('worker candidate business status mismatch')
    no_sdk_allowed = (payload['operation'] in ('prepare', 'consume') or payload['operation'] == 'observe' and payload.get('episode')
                      or payload['operation'] == 'build' and candidate['result'].get('status') == 'completed')
    if candidate.get('terminationMode') not in (('official_fast_shutdown', 'no_sdk_application') if no_sdk_allowed else ('official_fast_shutdown',)) or candidate.get('fullVendorCleanupVerified') is not False:
        raise ValueError('unsupported SDK termination contract')
    barriers = candidate.get('barriers', {})
    if set(barriers) != {'records', 'images', 'encoder', 'replicator'} or not all(value is True for value in barriers.values()):
        raise ValueError('worker data write barrier incomplete')
    root = Path(payload['root']).resolve()
    required = {str(Path(path).resolve().relative_to(root)) for path in required_files(payload, candidate['result'])}
    if not required or set(candidate.get('files', {})) != required:
        raise ValueError('worker candidate fixed operation file set mismatch')
    for name, expected in candidate['files'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or file_digest(path) != expected:
            raise ValueError(f'worker artifact missing, truncated or digest mismatch: {name}')
    return candidate['result']


def assign_device(device, environment):
    if (not isinstance(device, dict) or set(device) != {'index', 'uuid'}
            or type(device['index']) is not int or device['index'] < 0
            or not isinstance(device['uuid'], str) or not device['uuid'].startswith('GPU-')
            or len(device['uuid']) != 40):
        raise ValueError('worker requires an explicit physical GPU index and UUID')
    query = subprocess.run(['nvidia-smi', '--query-gpu=index,uuid,driver_version', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=10, check=True)
    devices = [tuple(value.strip() for value in line.split(',')) for line in query.stdout.splitlines()]
    selected = [row for row in devices if row[:2] == (str(device['index']), device['uuid'])]
    if len(selected) != 1:
        raise ValueError('assigned GPU index does not match its actual UUID')
    driver = selected[0][2] if len(selected[0]) == 3 else ''
    if len(driver.split('.')) < 2 or not all(part.isdecimal() for part in driver.split('.')):
        raise ValueError('assigned GPU driver version is unavailable or invalid')
    environment.update(CUDA_VISIBLE_DEVICES=device['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                       SCENEAGENT_GPU=str(device['index']))
    return driver


@contextmanager
def worker_gpu_lock(root, device):
    """Serialize one physical GPU; retain exclusion with legacy global workers."""
    import fcntl
    root = Path(root)
    legacy = root/'runtime/isaac-gpu.lock'
    legacy.parent.mkdir(parents=True, exist_ok=True)
    with legacy.open('a') as compatibility:
        if device is None:
            fcntl.flock(compatibility, fcntl.LOCK_EX)
            yield
            return
        if (not isinstance(device, dict) or set(device) != {'index', 'uuid'}
                or type(device['index']) is not int or device['index'] < 0
                or not isinstance(device['uuid'], str) or len(device['uuid']) != 40
                or not device['uuid'].startswith('GPU-')):
            raise ValueError('worker requires an explicit physical GPU index and UUID')
        name = valid_id(device['uuid'])
        # Legacy supervisors hold this file exclusively and may clean all old
        # workers. Shared ownership prevents them running alongside new workers.
        fcntl.flock(compatibility, fcntl.LOCK_SH)
        with (legacy.parent/('isaac-'+name+'.lock')).open('a') as selected:
            fcntl.flock(selected, fcntl.LOCK_EX)
            yield


def stop_previous_gpu_workers(root, directory, device):
    """Only the admitted GPU's orphan processes belong to this supervisor."""
    from runtime.gpu_reservation import process_lease

    jobs = Path(root)/'jobs'
    if not jobs.is_dir():
        return
    for job in jobs.iterdir():
        previous = job/'worker-process.json'
        if job == directory or not previous.is_file():
            continue
        identity = json.loads(previous.read_text())
        if process_lease(identity['pid']) and _token(identity['pid']) == identity.get('processToken'):
            continue
        if device is not None:
            request = previous.with_name('worker-request.json')
            recorded = json.loads(request.read_text()).get('device') if request.exists() else None
            if not isinstance(recorded, dict) or not recorded.get('uuid'):
                if identity.get('processToken') and _token(identity['pid']) == identity['processToken']:
                    raise RuntimeError('live previous worker has unknown GPU; cleanup requires inspection')
                continue
            if recorded['uuid'] != device['uuid']:
                continue
        stop_process_group(identity)


def supervise(payload, required_files, directory=None, command=None, timeout=None):
    """CPU parent validates only after the single Kit process actually ends."""
    selected_timeout = worker_timeout(payload)
    if timeout is not None:
        timeout = worker_timeout({'workerTimeoutSeconds': timeout})
        if 'workerTimeoutSeconds' in payload and timeout != selected_timeout:
            raise ValueError('worker timeout overrides conflict')
        payload = {**payload, 'workerTimeoutSeconds': timeout}
    else:
        timeout = selected_timeout
    root = Path(payload['root']).resolve()
    if directory is None:
        directory = root / 'runtime' / 'invocations' / (str(time.time_ns()))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    performance = (_parent_timing if _parent_timing is not None and _parent_timing['directory'] == directory
                   else _parent_performance(directory, payload, 'worker_verification_only'))
    performance['payload'] = payload
    performance['counters']['workerTimeoutSeconds'] = timeout
    atomic_json(directory / 'worker-request.json', payload)
    atomic_json(directory / 'worker-parent.json', {'pid': os.getpid(), 'processToken': _token(os.getpid())})
    from contextlib import ExitStack
    from runtime.consumption import frozen_worker, verify_frozen_candidate
    from runtime.gpu_reservation import configured_peak, reserve_gpu
    peak_mib = configured_peak(root, payload)
    native_command = command is None
    historical = frozen_worker(payload, root) if native_command else None
    with ExitStack() as admission:
        lease = None
        with measure('gpuLockWait', performance['counters']):
            if peak_mib is not None:
                lease = admission.enter_context(reserve_gpu(root, payload.get('device'), peak_mib, legacy_barrier=True,
                                                          allow_gpu3=payload.get('allowGpu3') is True))
            else:
                admission.enter_context(worker_gpu_lock(root, payload.get('device')))
        if lease is None:
            # Legacy supervisors own the entire GPU and may clean their old workers.
            stop_previous_gpu_workers(root, directory, payload.get('device'))
        if native_command:
            executable = historical['runtimeExecution']['python'] if historical else sys.executable
            script = root/historical['runtimeSnapshotRef']/'runtime/worker.py' if historical else Path(__file__).with_name('worker.py')
            command = [executable, str(script), '--kit-worker', str(directory),
                       '--portable', '--portable-root', str(directory / 'kit'), f'--/log/file={directory / "isaac-sdk.log"}']
        environment = dict(os.environ)
        environment.pop('CUDA_VISIBLE_DEVICES', None)
        if native_command or 'device' in payload:
            driver = assign_device(payload.get('device'), environment)
        if native_command:
            shaders = root/'cache/isaac-6-trajectory-v5/rtx'/payload['device']['uuid']/driver
            for setting, folder in (('shaderCachePath', 'shadercache'), ('driverShaderCachePath', 'nv_shadercache')):
                destination, current = shaders/folder, root
                for part in destination.relative_to(root).parts:
                    current = current/part
                    if current.is_symlink() or not current.resolve().is_relative_to(root):
                        raise ValueError('native shader cache must remain inside the plain configured cache root')
                    current.mkdir(exist_ok=True)
                command.append('--/rtx/shaderDb/'+setting+'='+str(destination))
        environment.update(OMNI_KIT_ACCEPT_EULA='YES', PYTHONFAULTHANDLER='1', PYTHONUNBUFFERED='1')
        for key, folder in [('XDG_CACHE_HOME', 'xdg-cache'), ('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                            ('CUDA_CACHE_PATH', 'cuda-cache'), ('__GL_SHADER_DISK_CACHE_PATH', 'gl-cache'), ('TMPDIR', 'tmp')]:
            destination = directory / folder
            destination.mkdir(exist_ok=True)
            environment[key] = str(destination)
        environment['PYTHONNOUSERSITE'] = '1'
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        for key, folder in (('WARP_CACHE_PATH', 'warp'), ('TORCH_EXTENSIONS_DIR', 'torch-extensions')):
            destination = root/'cache/isaac-6-trajectory-v5'/folder
            destination.mkdir(parents=True, exist_ok=True)
            environment[key] = str(destination)
        with (directory / 'kit.log').open('ab') as log:
            process = identity = worker_started = observed_exit = observed_code = primary = None

            def read_identity():
                if identity.get('processToken') is None:
                    token = _token(process.pid)
                    # A just-exited unreaped child still belongs to this Popen. Its
                    # Linux start token remains readable even while _token hides Z.
                    if token is None and sys.platform == 'linux':
                        try:
                            fields = Path(f'/proc/{process.pid}/stat').read_text().split(') ', 1)[1].split()
                            if (int(fields[1]) == os.getpid() and int(fields[2]) == process.pid
                                    and fields[19].isdecimal() and int(fields[19]) > 0):
                                token = fields[19]
                        except FileNotFoundError:
                            pass
                    identity['processToken'] = token
                identity['members'].update(_group_members(process.pid))
                if identity.get('processToken') is None:
                    raise RuntimeError('created worker identity unavailable; group cleanup unconfirmed')

            try:
                with terminal_lock(directory):
                    status_path = directory / 'status.json'
                    if status_path.exists() and json.loads(status_path.read_text())['state'] not in ('queued', 'running'):
                        raise RuntimeError('job became terminal before Isaac worker start')
                    worker_started = time.perf_counter()
                    child_options = lease.child_options(environment) if lease else {'env': environment}
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                               start_new_session=True, close_fds=True, **child_options)
                    identity = {'pid': process.pid, 'members': {}}
                    if lease:
                        identity['gpuReservationId'] = lease.id
                    read_identity()
                    atomic_json(directory / 'worker-process.json', identity)
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    members = _group_members(process.pid)
                    if any(identity['members'].get(pid) != token for pid, token in members.items()):
                        identity['members'].update(members)
                        atomic_json(directory / 'worker-process.json', identity)
                    if time.monotonic() >= deadline:
                        detail = _timeout_phase(directory, payload, identity)
                        error = TimeoutError('Isaac worker wall-clock budget exceeded; '+detail)
                        error.add_note(detail)
                        raise error
                    time.sleep(.1)
                observed_code = process.wait()
                if type(observed_code) is not int:
                    raise RuntimeError('worker wait did not return an actual exit code')
                observed_exit = time.perf_counter()
                remaining = _group_members(process.pid)
                if remaining:
                    identity['members'].update(remaining)
                    raise RuntimeError('Isaac worker left running child processes')
                if lease:
                    stop_process_group(identity, process)
                    lease.finished = True
                    admission.close()
                with measure('verification', performance['counters']):
                    if historical and observed_code == 0:
                        return verify_frozen_candidate(historical, directory, payload, observed_code)[0]
                    return verify_candidate(directory, payload, observed_code, required_files)
            except BaseException as error:
                primary = error
                raise
            finally:
                if lease and process is None:
                    lease.finished = True
                if process is not None:
                    cleanup_errors = []
                    cleanup_code = cleanup_completed = None
                    stopped = False
                    try:
                        if identity.get('processToken') is None:
                            read_identity()
                        if identity.get('processToken') is not None:
                            stop_process_group(identity, process)
                        elif process.poll() is None or identity['members']:
                            raise RuntimeError('created worker identity unavailable; group cleanup unconfirmed')
                        stopped = True
                    except BaseException as error:
                        cleanup_errors.append(error)
                    try:
                        # The process group can disappear before waitpid observes the exit.
                        reap_timeout = .25 if stopped and process.poll() is None else 0
                        cleanup_code = process.wait(timeout=reap_timeout)
                        if type(cleanup_code) is not int:
                            raise RuntimeError('cleanup wait did not return an actual exit code')
                        if stopped:
                            cleanup_completed = time.perf_counter()
                            if lease:
                                lease.finished = True
                    except BaseException as error:
                        cleanup_errors.append(error)
                    try:
                        if cleanup_errors:
                            performance['counters']['diagnosticIncomplete'] = True
                        wall_end = observed_exit if observed_exit is not None else time.perf_counter()
                        performance['counters']['workerWall'] = {'seconds': wall_end - worker_started,
                            'count': 1, 'failedCount': int(primary is not None or bool(cleanup_errors) or observed_code != 0),
                            'measurement': 'observed_upper_bound'}
                        with terminal_lock(directory):
                            status_path = directory / 'status.json'
                            status = json.loads(status_path.read_text()) if status_path.exists() else None
                            if status is None or status['state'] in ('queued', 'running'):
                                _write_performance(directory, performance, identity=identity,
                                    exit_code=observed_code, observed_exit=observed_exit,
                                    cleanup_return_code=cleanup_code if observed_exit is None else None,
                                    cleanup_completed=cleanup_completed if observed_exit is None else None)
                    except BaseException as error:
                        cleanup_errors.append(error)
                    if primary is not None:
                        for error in cleanup_errors:
                            primary.add_note('worker cleanup/diagnostic pid='+str(process.pid)+' token='+repr(identity.get('processToken'))+': '+repr(error))
                    elif cleanup_errors:
                        first, *others = cleanup_errors
                        for error in others:
                            first.add_note('worker cleanup/diagnostic: '+repr(error))
                        raise first


def append_event(directory, event):
    import fcntl
    with (directory / 'events.jsonl').open('a+') as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        file.seek(0)
        sequence = sum(1 for _ in file) + 1
        file.write(json.dumps({'sequence': sequence, **event}, ensure_ascii=False, allow_nan=False) + '\n')
        file.flush()
        os.fsync(file.fileno())
    return sequence


def submit(snapshot, root, request):
    def request_scope(value):
        return [valid_id(value[name]) if value.get(name) is not None else None
                for name in ('projectId', 'generationId')] + [valid_id(value['requestId'])]
    root, snapshot = Path(root), Path(snapshot)
    def stored_request(job_id, label):
        try:
            job_id = valid_id(job_id)
            saved = json.loads((root / 'jobs' / job_id / 'request.json').read_text())
            original = saved['payload']
            if not isinstance(original, dict):raise ValueError('stored request payload is not an object')
            stored_scope = request_scope(original)
            stored_hash = hashlib.sha256(json.dumps({key:value for key,value in original.items() if key!='jobId'},
                                                    sort_keys=True, allow_nan=False).encode()).hexdigest()
            if original.get('jobId') != job_id or saved.get('requestHash') != stored_hash:
                raise ValueError('stored request identity or digest differs')
            return stored_scope, stored_hash
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(label+' request scope evidence is missing or inconsistent') from exc
    id = valid_id(request['jobId'])
    request_id = valid_id(request['requestId'])
    directory = root / 'jobs' / id
    directory.parent.mkdir(parents=True, exist_ok=True)
    digest_input = {key: value for key, value in request.items() if key != 'jobId'}
    request_hash = hashlib.sha256(json.dumps(digest_input, sort_keys=True, allow_nan=False).encode()).hexdigest()
    identities = root / 'jobs' / 'requests'
    identities.mkdir(exist_ok=True)
    scope = request_scope(request)
    scoped = identities / 'v2'
    scoped.mkdir(exist_ok=True)
    identity = scoped / (hashlib.sha256(json.dumps(scope, separators=(',', ':')).encode()).hexdigest() + '.json')
    import fcntl
    with (identities / (request_id + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        legacy = identities / (request_id + '.json')
        if not identity.exists() and legacy.exists():
            try:
                previous = json.loads(legacy.read_text())
                old_id = valid_id(previous['jobId'])
                old_scope, old_hash = stored_request(old_id, 'legacy')
                if old_scope[-1] != request_id or previous.get('requestHash') != old_hash:
                    raise ValueError('legacy request identity or digest differs')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError('legacy request scope evidence is missing or inconsistent') from exc
            if old_scope == scope:
                if old_hash != request_hash:raise ValueError('request ID reused with different input')
                atomic_json(identity, {'jobId':old_id, 'requestHash':old_hash, 'scope':scope})
        if identity.exists():
            previous = json.loads(identity.read_text())
            if previous.get('scope') != scope:raise ValueError('scoped request identity differs')
            if previous['requestHash'] != request_hash:
                raise ValueError('request ID reused with different input')
            if previous['jobId'] != id:
                if stored_request(previous['jobId'], 'scoped') != (scope, request_hash):
                    raise ValueError('scoped index differs from actual job scope or digest')
                return inspect(root, valid_id(previous['jobId']))
        else:
            if directory.exists() and stored_request(id, 'job') != (scope, request_hash):
                raise ValueError('job ID reused with different input or scope')
            atomic_json(identity, {'jobId': id, 'requestHash': request_hash, 'scope':scope})
        try:
            directory.mkdir()
        except FileExistsError:
            # A partially published request is not silently submitted twice.
            if stored_request(id, 'job') != (scope, request_hash):
                raise ValueError('job ID reused with different input or scope')
            return inspect(root, id)
        atomic_json(directory / 'request.json', {'requestHash': request_hash, 'payload': request,
                                               'runtimeSnapshot': str(snapshot)})
        atomic_json(directory / 'status.json', {'jobId': id, 'requestId': request_id, 'state': 'queued'})
        (directory / 'events.jsonl').touch()
        append_event(directory, {'type': 'submitted', 'jobId': id})
        with terminal_lock(directory):
            status = json.loads((directory / 'status.json').read_text())
            if status['state'] == 'queued':
                from runtime.simulation import _cpu_environment
                with (directory / 'process.log').open('ab') as log:
                    process = subprocess.Popen([sys.executable, str(snapshot / 'worker.py'), '--execute-job', str(directory)],
                                               stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                               start_new_session=True, close_fds=True, env=_cpu_environment())
                import threading
                threading.Thread(target=process.wait, daemon=True).start()
                atomic_json(directory / 'status.json', {'jobId': id, 'requestId': request_id, 'state': 'running',
                                                       'pid': process.pid, 'processToken': _token(process.pid),
                                                       'startedAt': time.time()})
        return inspect(root, id)


def inspect(root, id, action='status', cursor=0, wait_seconds=0):
    if action not in ('status', 'wait', 'cancel'):
        raise ValueError('unknown job action')
    directory = Path(root) / 'jobs' / valid_id(id)
    deadline = time.monotonic() + min(30, max(0, float(wait_seconds)))
    while True:
        status = json.loads((directory / 'status.json').read_text())
        if action == 'cancel':
            with terminal_lock(directory):
                status = json.loads((directory / 'status.json').read_text())
                if status['state'] in ('queued', 'running'):
                    worker = _stop_worker(directory)
                    stop_process_group(status)
                    _release_stopped_reservation(root, directory, status, worker)
                    (directory / 'result.json').unlink(missing_ok=True)
                    status.update(state='cancelled', finishedAt=time.time(), error='cancelled by user')
                    atomic_json(directory / 'status.json', status)
                    append_event(directory, {'type': 'cancelled', 'jobId': id})
                    _write_performance(directory, status=status)
        elif status['state'] == 'running' and not (
                isinstance(status.get('processToken'), str) and status['processToken']
                and status.get('pid') and _token(status['pid']) == status['processToken']):
            with terminal_lock(directory):
                status = json.loads((directory / 'status.json').read_text())
                if status['state'] == 'running':
                    worker = _stop_worker(directory)
                    _release_stopped_reservation(root, directory, status, worker)
                    status.update(state='failed', error='remote process ended without a terminal receipt', finishedAt=time.time())
                    (directory / 'result.json').unlink(missing_ok=True)
                    atomic_json(directory / 'status.json', status)
                    append_event(directory, {'type': 'error', 'error': status['error']})
                    _write_performance(directory, status=status)
        if status['state'] in ('completed', 'failed', 'cancelled'):
            with terminal_lock(directory):
                status = json.loads((directory / 'status.json').read_text())
                if status['state'] in ('completed', 'failed', 'cancelled'):
                    _reconcile_terminal_reservation(root, directory, status)
        events = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]
        if action != 'wait' or status['state'] not in ('queued', 'running') or any(row['sequence'] > cursor for row in events) or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    result_path = directory / 'result.json'
    return {**status, 'events': [row for row in events if row['sequence'] > int(cursor)],
            'cursor': events[-1]['sequence'] if events else 0,
            'result': json.loads(result_path.read_text()) if status['state'] == 'completed' and result_path.exists() else None}


def execute(directory, runner, finalize=None):
    global _parent_timing
    directory = Path(directory)
    deadline = time.monotonic() + 5
    while json.loads((directory / 'status.json').read_text())['state'] == 'queued' and time.monotonic() < deadline:
        time.sleep(.01)
    envelope = json.loads((directory / 'request.json').read_text())
    request = envelope['payload']
    payload = {**request, **request['task']}
    payload['operation'] = request['task']['operation']
    previous = _parent_timing
    performance = _parent_performance(directory, payload, 'job')
    performance['runtimeSnapshot'] = envelope.get('runtimeSnapshot')
    _parent_timing = performance
    try:
        with measure('supervision', performance['counters']):
            result = runner(payload)
        _terminal(directory, 'completed', result=result,
                  finalize=(lambda value: finalize(payload, value)) if finalize else None, performance=performance)
    except BaseException as exc:
        import traceback
        atomic_json(directory / 'failure.json', {'error': f'{type(exc).__name__}: {exc}', 'traceback': traceback.format_exc()})
        _terminal(directory, 'failed', error=f'{type(exc).__name__}: {exc}', performance=performance)
    finally:
        _parent_timing = previous
