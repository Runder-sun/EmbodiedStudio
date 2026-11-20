"""One committed named-I/O stream; bulk arrays and cameras retain their physical clocks."""
from __future__ import annotations

import json
from collections import ChainMap
from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

CAMERA_TIME_TOLERANCE = 1e-7




def atomic_json(path, value):
    descriptor, filename = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, 'w') as file:
            json.dump(value, file, ensure_ascii=False, allow_nan=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_publication(manifest, root):
    root = Path(root).resolve()
    job = manifest.get('publicationJobId')
    if not job or not isinstance(job, str) or '/' in job or '..' in job:
        raise ValueError('artifact lacks a verified job publication')
    directory = root / 'jobs' / job
    status_path, result_path = directory / 'status.json', directory / 'result.json'
    if not status_path.is_file():
        raise ValueError('artifact job has not committed a verified successful exit')
    status = json.loads(status_path.read_text())
    if status.get('state') != 'completed' or status.get('jobId') != job or not result_path.is_file():
        raise ValueError('artifact job has not committed a verified successful exit')
    result = json.loads(result_path.read_text())
    if result.get('projectId') != manifest.get('projectId') or result.get('revision') != manifest.get('revision'):
        raise ValueError('publication job belongs to another scene revision')
    if manifest.get('episodeId'):
        matches = [row for row in result.get('episodes', []) if row.get('episode') == manifest['episodeId']]
        if len(matches) != 1 or matches[0].get('taskId') != manifest['taskId']:
            raise ValueError('publication job does not contain this episode')
    reference = manifest['artifacts']['manifest']
    path = (root / reference).resolve()
    expected = result.get('publicationArtifactDigests', {}).get(reference)
    if not path.is_relative_to(root) or not path.is_file() or not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError('published manifest digest mismatch')


def _finite_vector(value, length, label):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError('invalid physical sample '+label)
    try:
        finite = all(type(x) in (int, float) and math.isfinite(x) for x in value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError('invalid physical sample '+label)
    return value


def _physical_quaternion(raw):
    value = _finite_vector(raw, 4, 'raw wxyz orientation')
    length = math.hypot(*value)
    if not length or abs(length-1) > 1e-6:
        raise ValueError('raw native quaternion exceeds the representation contract')
    return [float(x)/length for x in value]


def _physical_matrix(raw, *, rigid=False):
    import numpy as np
    import trimesh
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError('invalid raw native matrix')
    for row in raw:
        _finite_vector(row, 4, 'raw matrix')
    if raw[3] != [0, 0, 0, 1]:
        raise ValueError('invalid raw homogeneous matrix')
    matrix = np.asarray(raw, dtype=np.float64)
    if not rigid:
        return matrix
    q = _physical_quaternion(trimesh.transformations.quaternion_from_matrix(matrix).tolist())
    rebuilt = trimesh.transformations.quaternion_matrix(q)
    if float(np.max(np.abs(matrix[:3, :3]-rebuilt[:3, :3]))) > 1e-6:
        raise ValueError('raw TCP/link matrix is not a rigid representation within tolerance')
    return {'position': matrix[:3, 3].tolist(), 'orientation': q}


def _physical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def validate_io(io):
    """Validate structure, not SDK qualification; the frozen worker supplies provenance."""
    import numpy as np
    if not isinstance(io, dict) or set(io) != {'schema', 'clock', 'ports', 'streams'} or io['schema'] != 'sceneagent-native-io-v1':
        raise ValueError('unsupported native I/O contract')
    clock = io['clock']
    if (not isinstance(clock, dict) or set(clock) != {'physicsRateHz', 'controlRateHz'}
            or any(type(value) is not int or value <= 0 for value in clock.values())
            or clock['physicsRateHz'] % clock['controlRateHz']):
        raise ValueError('native I/O requires explicit integer physics/control clocks')
    for kind in ('ports', 'streams'):
        if not isinstance(io[kind], dict):raise ValueError('native I/O requires named '+kind)
        for name, spec in io[kind].items():
            if (not isinstance(name, str) or not name or not isinstance(spec, dict)
                    or not isinstance(spec.get('source'), dict) or not spec['source']
                    or not isinstance(spec.get('shape'), list)
                    or any(axis is not None and (type(axis) is not int or axis < 0) for axis in spec['shape'])):
                raise ValueError('native I/O binding or shape is invalid')
            try:dtype = np.dtype(spec['dtype'])
            except (KeyError, TypeError):raise ValueError('native I/O dtype is invalid') from None
            if dtype.kind not in 'buif' or dtype.hasobject:
                raise ValueError('native I/O supports only plain numeric/bool data')
            units = spec.get('units')
            if not (isinstance(units, str) and units or isinstance(units, list) and units and all(isinstance(unit, str) and unit for unit in units)):
                raise ValueError('native I/O units must be explicit')
            if kind == 'ports':
                if any(axis is None or axis < 1 for axis in spec['shape']) or not isinstance(spec.get('persistence'), str):
                    raise ValueError('native command ports require fixed shape and persistence')
                limits = spec.get('limits')
                size = math.prod(spec['shape'])
                if (not isinstance(limits, list) or len(limits) != size or any(
                        not isinstance(pair, list) or len(pair) != 2
                        or any(value is not None and (type(value) not in (int, float) or not math.isfinite(value)) for value in pair)
                        or pair[0] is not None and pair[1] is not None and pair[0] >= pair[1] for pair in limits)):
                    raise ValueError('native command ports require explicit frozen limits')
            elif (spec.get('origin') not in ('native_output', 'authored_configuration')
                    or type(spec.get('visibleToPolicy')) is not bool or type(spec.get('required')) is not bool
                    or type(spec.get('rateHz')) is not int or spec['rateHz'] <= 0
                    or clock['physicsRateHz'] % spec['rateHz'] or not isinstance(spec.get('frame'), str)):
                raise ValueError('native stream requires frozen source/origin, sampling and permission')
    json.dumps(io, allow_nan=False)
    return io


def has_control_ports(io):
    """Structural minimum only; callers must verify the frozen SDK I/O identity."""
    return bool(validate_io(io)['ports'])


def _typed_array(value, spec):
    import numpy as np
    dtype = np.dtype(spec['dtype'])
    def bool_value(item):
        return isinstance(item, (bool, np.bool_)) or isinstance(item, (list, tuple)) and any(bool_value(child) for child in item)
    if dtype.kind != 'b' and bool_value(value):raise ValueError('boolean is not a numeric native value')
    try:
        array = np.asarray(value)
        if array.dtype.kind not in 'buif' or array.dtype.hasobject:raise ValueError('native array must have plain numeric dtype')
        if isinstance(value, np.ndarray) and array.dtype != dtype:raise ValueError('native array dtype differs from frozen stream')
        converted = array.astype(dtype, copy=False)
    except (TypeError, OverflowError):raise ValueError('native value has invalid dtype') from None
    if len(converted.shape) != len(spec['shape']) or any(expected is not None and actual != expected for actual, expected in zip(converted.shape, spec['shape'])):
        raise ValueError('native value shape differs from frozen I/O')
    if not np.isfinite(converted).all():raise ValueError('native value must be finite')
    if dtype.kind in 'iu' and not np.array_equal(array, converted):raise ValueError('native integer conversion changes its value')
    return converted


def validate_action(action, io):
    if not isinstance(action, dict) or set(action)-set(io['ports']):raise ValueError('command contains an unknown native port')
    for name, value in action.items():
        array = _typed_array(value, io['ports'][name])
        for scalar, (low, high) in zip(array.reshape(-1), io['ports'][name]['limits']):
            if low is not None and scalar < low or high is not None and scalar > high:
                raise ValueError('native command outside frozen limits: '+name)
    return action


def validate_protocol(manifest, schema, *, native=True):
    if manifest.get('schema') != schema or manifest.get('engine') != 'isaacsim' or manifest.get('physics') != 'physx':
        raise ValueError('unsupported simulation protocol; use the package frozen worker and interpreter')
    validate_io(manifest.get('io'))


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):digest.update(block)
    return digest.hexdigest()


def _plain_file_identity(directory, ref):
    root = Path(directory).resolve()
    if not isinstance(ref, str) or not PurePosixPath(ref).parts or PurePosixPath(ref).is_absolute() or '..' in PurePosixPath(ref).parts:
        raise ValueError('array reference is outside episode scope')
    parts = PurePosixPath(ref).parts
    path = root
    for index, part in enumerate(parts):
        path = path/part
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError('array reference is outside episode scope')
        if not (stat.S_ISDIR(mode) if index < len(parts)-1 else stat.S_ISREG(mode)):
            raise ValueError('array reference must use ordinary directories and one regular file')
    before = path.stat()
    return path, (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)


def read_array(directory, reference, *, _cache=None):
    import numpy as np
    if not isinstance(reference, dict) or set(reference) != {'ref', 'sha256', 'dtype', 'shape'}:
        raise ValueError('array reference requires path, digest, dtype and shape')
    ref = reference['ref']
    if not isinstance(ref, str) or len(PurePosixPath(ref).parts) < 2 or PurePosixPath(ref).parts[0] != 'arrays':
        raise ValueError('array reference is outside episode scope')
    path, identity = _plain_file_identity(directory, ref)
    key = ('read', str(path), reference['sha256'], reference['dtype'], tuple(reference['shape']))
    cached = _cache.get(key) if _cache is not None else None
    if cached is not None:
        if cached[0] != identity:
            raise ValueError('array artifact identity or digest changed during validation')
        _cache[key] = cached
        return cached[1]
    if _file_digest(path) != reference['sha256']:
        raise ValueError('array artifact digest mismatch')
    try:array = np.load(path, allow_pickle=False, mmap_mode='r')
    except (OSError, ValueError, TypeError):raise ValueError('array artifact is not a plain native array') from None
    if not isinstance(array, np.ndarray):
        if callable(getattr(array, 'close', None)):array.close()
        raise ValueError('array artifact must be one plain NPY array')
    if (array.dtype.kind not in 'buif' or array.dtype.hasobject or str(array.dtype) != reference['dtype']
            or list(array.shape) != reference['shape'] or not np.isfinite(array).all()):
        raise ValueError('array artifact dtype/shape/value mismatch')
    after = path.stat()
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('array artifact changed during validation')
    if _cache is not None:
        _cache[key] = (identity, array)
    return array


def array_references(value):
    """Walk references in selected facts; callers choose the authorized fact scope."""
    if isinstance(value, dict):
        if set(value) == {'ref','sha256','dtype','shape'}:
            yield value
        else:
            for child in value.values():yield from array_references(child)
    elif isinstance(value, list):
        for child in value:yield from array_references(child)


def _array_reference(directory, array, *, _cache=None):
    import numpy as np
    root = Path(directory)/'arrays'; root.mkdir(exist_ok=True)
    if root.is_symlink():raise ValueError('array output directory must be episode-owned')
    key = None
    if _cache is not None:
        content = np.ascontiguousarray(array)
        key = ('freeze', str(root.resolve()), str(array.dtype), array.shape,
            hashlib.sha256(memoryview(content).cast('B') if content.size else b'').hexdigest())
        if key in _cache:
            reference = _cache[key]
            read_array(directory, reference, _cache=_cache)
            _cache[key] = reference
            return deepcopy(reference)
    descriptor, name = tempfile.mkstemp(prefix='array.', suffix='.tmp', dir=root)
    try:
        with os.fdopen(descriptor, 'wb') as file:
            np.save(file, array, allow_pickle=False); file.flush(); os.fsync(file.fileno())
        sha = _file_digest(name); target = root/(sha+'.npy')
        if target.exists():
            if target.is_symlink() or _file_digest(target) != sha:raise ValueError('immutable array artifact differs')
        else:Path(name).replace(target)
    finally:Path(name).unlink(missing_ok=True)
    reference = {'ref':str(target.relative_to(directory)), 'sha256':sha, 'dtype':str(array.dtype), 'shape':list(array.shape)}
    if _cache is not None:
        _cache[key] = reference
    return reference


def _clock(value):
    if (not isinstance(value, dict) or any(type(value.get(key)) not in (int, float) or not math.isfinite(value[key]) for key in ('time','simulationTime','timeOrigin'))
            or type(value.get('physicsStep')) is not int or value['physicsStep'] < 0 or value.get('stageId') is None
            or abs(value['simulationTime']-value['timeOrigin']-value['time']) > 1e-9):
        raise ValueError('observation physical clock/origin is invalid')


def validate_streams(value, io, *, truth=False, directory=None, write=False, selected=None, _array_cache=None):
    """Check frozen source metadata; values originate only in the trusted runtime caller."""
    _clock(value)
    if set(value)-{'time','simulationTime','timeOrigin','physicsStep','stageId','streams','streamStatus','cameraFrames'}:
        raise ValueError('native facts must use declared streams, not claimed top-level measurements')
    if abs(value['time']-value['physicsStep']/io['clock']['physicsRateHz']) > 1e-9:raise ValueError('observation physical clock differs from frozen I/O')
    names = set(io['streams']) if truth else {name for name, spec in io['streams'].items() if spec['visibleToPolicy']}
    if selected is not None:names &= set(selected)
    values, statuses = value.get('streams'), value.get('streamStatus', {})
    if not isinstance(values, dict) or not isinstance(statuses, dict) or set(values)-names or set(statuses)-names:
        raise ValueError('unknown or unauthorized native stream')
    result = deepcopy(value); result['streams'] = {}
    for name in names:
        status = statuses.get(name, {'status':'available'})
        if not isinstance(status, dict) or set(status)-{'status','reason','sampleTime','physicsStep'}:
            raise ValueError('stream payload cannot override frozen source/origin')
        if status.get('status') != 'available':
            if (status.get('status') not in ('unavailable','unverified','unknown') or not isinstance(status.get('reason'), str) or not status['reason']
                    or name in values or io['streams'][name]['required']):
                raise ValueError('required native stream unavailable or failure is not explicit')
            continue
        if name not in values:raise ValueError('native stream is missing without availability evidence')
        sample = status.get('sampleTime', value['time'])
        if type(sample) not in (int, float) or not math.isfinite(sample) or not 0 <= sample <= value['time']+1e-9:
            raise ValueError('future or invalid native stream sample time')
        sample_step = status.get('physicsStep', value['physicsStep'])
        if type(sample_step) is not int or not 0 <= sample_step <= value['physicsStep'] or abs(sample-sample_step/io['clock']['physicsRateHz']) > 1e-9:
            raise ValueError('stream sample time differs from its physical step')
        item = values[name]; spec = io['streams'][name]
        if isinstance(item, dict) and 'ref' in item:
            if directory is None:raise ValueError('native array validation requires episode scope')
            _typed_array(read_array(directory, item, _cache=_array_cache), spec); result['streams'][name] = deepcopy(item)
        else:
            array = _typed_array(item, spec)
            if array.size > 64 or array.size == 0:
                if not write or directory is None:raise ValueError('large native arrays require committed artifact references')
                result['streams'][name] = _array_reference(directory, array, _cache=_array_cache)
            else:result['streams'][name] = array.tolist()
    return result


def _validate_boundary(observed, truth):
    if any(observed[key] != truth[key] for key in ('time','simulationTime','timeOrigin','physicsStep','stageId')):
        raise ValueError('native truth and policy physical boundary differ')
    for name, value in observed['streams'].items():
        if value != truth['streams'].get(name) or observed.get('streamStatus', {}).get(name) != truth.get('streamStatus', {}).get(name):
            raise ValueError('policy input boundary differs from raw source stream')


def _physical_streams(io, step=None):
    return {name for name,spec in io['streams'].items() if spec['rateHz'] > io['clock']['controlRateHz']
            and (step is None or step % (io['clock']['physicsRateHz']//spec['rateHz']) == 0)}


def _sample_status(value, name):
    status = deepcopy(value.get('streamStatus', {}).get(name, {'status':'available'}))
    if status['status'] == 'available':
        status.setdefault('sampleTime', value['time']); status.setdefault('physicsStep', value['physicsStep'])
    return status


def _validate_cycle(cycle, io, directory, *, _array_cache=None):
    if cycle.get('engine') != 'isaacsim' or cycle.get('physics') != 'physx':
        raise ValueError('transition engine differs from the frozen protocol')
    validate_action(cycle['targetAction'], io); validate_action(cycle['executedAction'], io)
    before, after = cycle['observation'], cycle['nextObservation']
    if (cycle['timeStart'] != before['time'] or cycle['timeEnd'] != after['time']
            or set(cycle['executedAction'])-set(cycle['targetAction'])):
        raise ValueError('recorded boundary or applied command differs from its request')
    for value, truth in ((before,False),(after,False),(cycle['truthBefore'],True),(cycle['truthAfter'],True)):
        validate_streams(value, io, truth=truth, directory=directory, _array_cache=_array_cache)
    steps = cycle['physicalSteps']
    if (type(steps) is not int or steps <= 0 or after['physicsStep']-before['physicsStep'] != steps
            or before['stageId'] != after['stageId'] or before['timeOrigin'] != after['timeOrigin']
            or abs(after['time']-before['time']-steps/io['clock']['physicsRateHz']) > 1e-9):
        raise ValueError('control transition changed stage or physical step/clock')
    for observed, truth in ((before,cycle['truthBefore']),(after,cycle['truthAfter'])):
        _validate_boundary(observed, truth)
    samples = cycle.get('physicalSamples')
    high_rate = _physical_streams(io)
    if high_rate and samples is None:raise ValueError('declared high-rate streams lack actual substep samples')
    if samples is not None:
        if not isinstance(samples, list) or len(samples) != steps:raise ValueError('physical sample count differs from actual substeps')
        latest = {name:cycle['truthBefore'] for name in high_rate}
        for index, sample in enumerate(samples,1):
            _clock(sample)
            due = _physical_streams(io, sample['physicsStep'])
            validate_streams(sample, io, truth=True, directory=directory, selected=due, _array_cache=_array_cache)
            if sample['physicsStep'] != before['physicsStep']+index or sample['stageId'] != before['stageId'] or sample['timeOrigin'] != before['timeOrigin']:
                raise ValueError('physical samples changed clock, origin or substep identity')
            for name in due:
                status = _sample_status(sample, name)
                if status['status'] == 'available' and (status['physicsStep'] != sample['physicsStep'] or abs(status['sampleTime']-sample['time']) > 1e-9):
                    raise ValueError('due physical stream uses a stale sample clock')
                latest[name] = sample
        for name, sample in latest.items():
            if (sample['streams'].get(name) != cycle['truthAfter']['streams'].get(name)
                    or _sample_status(sample, name) != _sample_status(cycle['truthAfter'], name)):
                raise ValueError('final raw stream differs from its latest physical sample')



def reset_facts(initial_snapshot):
    return {'phase': 'reset', 'initialSnapshot': deepcopy(initial_snapshot)}


def cycle_facts(row):
    keys = ('cycleId', 'timeStart', 'timeEnd', 'observation', 'nextObservation', 'targetAction',
            'executedAction', 'physicalSteps', 'events', 'truthBefore', 'truthAfter')
    facts = {'phase': 'transition', **{key: deepcopy(row[key]) for key in keys}}
    if 'physicalSamples' in row:
        facts['physicalSamples'] = deepcopy(row['physicalSamples'])
    return facts


def _score_preview(session, phase, facts):
    try:
        staged = session.preview(phase, deepcopy(facts))
        result = staged['result']
        if (set(staged) != {'state', 'result'} or set(result) != {'success', 'terminated', 'reward', 'details'}
                or result['success'] is not None and type(result['success']) is not bool
                or type(result['terminated']) is not bool
                or result['reward'] is not None and (type(result['reward']) not in (int, float)
                    or not math.isfinite(result['reward']))):
            raise ValueError('malformed generated rubric result')
        json.dumps(staged, allow_nan=False)
        return deepcopy(staged), 'available', None
    except Exception as error:
        return {'state': None, 'result': {'success': None, 'terminated': False, 'reward': None,
                'details': {'scoreUnavailable': True}}}, 'unavailable', {'type': type(error).__name__, 'message': str(error)}


class Recorder:
    def __init__(self, directory, *, io, recording=None, task=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.io = validate_io(deepcopy(io))
        from runtime.scene import validate_recording
        recording = validate_recording(deepcopy(recording) if recording is not None else {'cameras': [], 'primaryCameraId': None})
        self.file = (self.directory / 'transitions.jsonl').open('w')
        self.cycles, self.physical_steps, self.failed = 0, 0, False
        self.initial_snapshot, self.session, self._committed_observation = None, None, None
        self._array_cache = {}
        self.index = {'schema': 'sceneagent-control-transitions-v6', 'engine': 'isaacsim', 'physics': 'physx',
                      'io': self.io, 'recording': recording,
                      'task': {key: deepcopy(task[key]) for key in ('id', 'instruction', 'bindings', 'measurements')} if task is not None else None,
                      'committedCycles': 0, 'committedBytes': 0, 'physicalSteps': 0, 'status': 'initializing',
                      'terminated': False, 'truncated': False}
        atomic_json(self.directory / 'records.json', self.index)

    def _snapshot(self, snapshot, *, _array_cache=None):
        try:
            return {'observation': validate_streams(snapshot['observation'], self.io, directory=self.directory, write=True, _array_cache=_array_cache),
                    'truth': validate_streams(snapshot['truth'], self.io, truth=True, directory=self.directory, write=True, _array_cache=_array_cache)}
        except (OSError, ValueError, TypeError):
            self.failed = True
            raise

    @property
    def episode_done(self):
        return bool(self.failed or self.index.get('terminated') or self.index.get('truncated'))

    @property
    def last_result(self):
        return deepcopy(self.index.get('lastResult'))

    @property
    def committed_observation(self):
        return deepcopy(self._committed_observation)

    def begin(self, initial_snapshot, session):
        if self.session is not None or 'initialSnapshot' in self.index:
            raise ValueError('episode reset is already recorded; begin is allowed once')
        if self.failed or self.file.closed:
            raise RuntimeError('recorder failed or closed')
        if session is None or not callable(getattr(session, 'preview', None)) or not callable(getattr(session, 'accept', None)):
            raise ValueError('Recorder.begin requires the isolated rubric session')
        if not isinstance(initial_snapshot, dict) or set(initial_snapshot) != {'observation', 'truth'}:
            raise ValueError('initial snapshot requires actual observation and host truth')
        initial_snapshot = self._snapshot(initial_snapshot, _array_cache=self._array_cache)
        _validate_boundary(initial_snapshot['observation'], initial_snapshot['truth'])
        if initial_snapshot['observation']['physicsStep'] != 0:
            raise ValueError('initial snapshot must start at the actual reset origin')
        facts = reset_facts(initial_snapshot)
        staged, status, error = _score_preview(session, 'reset', facts)
        initial = {'facts': facts, 'factsDigest': _physical_digest(facts), **deepcopy(staged),
                   'scoreStatus': status, 'scoreError': error}
        next_index = dict(self.index, initialSnapshot=deepcopy(initial_snapshot),
                          initialSnapshotDigest=_physical_digest(initial_snapshot), rubric=deepcopy(session.descriptor),
                          initialRubric=initial, lastResult=deepcopy(staged['result']),
                          terminated=staged['result']['terminated'], truncated=status == 'unavailable',
                          status='running' if status == 'available' else 'score_unavailable')
        try:
            atomic_json(self.directory / 'records.json', next_index)
        except (OSError, ValueError, TypeError):
            self.failed = True
            raise
        self.index, self.session, self.initial_snapshot = next_index, session, deepcopy(initial_snapshot)
        if status == 'available':
            try:
                session.accept(staged)
            except Exception:
                self.failed = True
                raise
        self._committed_observation = deepcopy(next_index['initialSnapshot']['observation'])
        return self.last_result

    def commit(self, before, target_action, executed_action, after, physical_steps, events=(), truth_before=None,
               truth_after=None, physical_samples=None, *, truncated=False):
        if self.failed or self.file.closed:
            raise RuntimeError('recorder failed; close this episode')
        if self.session is None:
            raise RuntimeError('Recorder.begin must record the reset before a control cycle')
        if self.episode_done:
            raise RuntimeError('episode ended; no additional control cycles may be committed')
        validate_action(target_action, self.io)
        validate_action(executed_action, self.io)
        array_cache = ChainMap({}, self._array_cache)
        initial = self._snapshot({'observation': before, 'truth': truth_before}, _array_cache=array_cache)
        final = self._snapshot({'observation': after, 'truth': truth_after}, _array_cache=array_cache)
        expected_observation = self._committed_observation
        expected_truth = self.index['initialSnapshot']['truth'] if self.cycles == 0 else self._committed_truth
        if initial['observation'] != expected_observation or initial['truth'] != expected_truth:
            self.failed = True
            raise ValueError('control cycle differs from the committed initial/previous boundary')
        cycle = {'engine': 'isaacsim', 'physics': 'physx', 'cycleId': self.cycles,
                 'timeStart': before['time'], 'timeEnd': after['time'],
                 'observation': initial['observation'], 'nextObservation': final['observation'],
                 'targetAction': {name:_typed_array(value,self.io['ports'][name]).tolist() for name,value in target_action.items()},
                 'executedAction': {name:_typed_array(value,self.io['ports'][name]).tolist() for name,value in executed_action.items()},
                 'physicalSteps': physical_steps, 'events': deepcopy(list(events)),
                 'truthBefore': initial['truth'], 'truthAfter': final['truth']}
        try:
            if physical_samples is not None:
                cycle['physicalSamples'] = []
                for sample in physical_samples:
                    _clock(sample)
                    cycle['physicalSamples'].append(validate_streams(sample, self.io, truth=True, directory=self.directory,
                        write=True, selected=_physical_streams(self.io, sample['physicsStep']), _array_cache=array_cache))
            _validate_cycle(cycle, self.io, self.directory, _array_cache=array_cache)
        except (ValueError, TypeError, OSError):
            self.failed = True
            raise
        facts = cycle_facts(cycle)
        staged, status, error = _score_preview(self.session, 'transition', facts)
        cycle.update(facts=facts, factsDigest=_physical_digest(facts), rubricResult=deepcopy(staged['result']),
                     rubricState=deepcopy(staged['state']), scoreStatus=status, scoreError=error,
                     terminated=staged['result']['terminated'], truncated=bool(truncated or status == 'unavailable'))
        from runtime.jobs import measure
        try:
            with measure('recordWrite'):
                self.file.write(json.dumps(cycle, ensure_ascii=False, allow_nan=False)+'\n')
                self.file.flush()
                os.fsync(self.file.fileno())
            next_index = dict(self.index, committedCycles=self.cycles+1, committedBytes=self.file.tell(),
                              physicalSteps=self.physical_steps+physical_steps, lastResult=deepcopy(staged['result']),
                              terminated=cycle['terminated'], truncated=cycle['truncated'])
            with measure('recordIndex'):
                atomic_json(self.directory / 'records.json', next_index)
        except (OSError, ValueError, TypeError):
            self.failed = True
            raise
        self.index = next_index
        self.cycles += 1
        self.physical_steps += physical_steps
        if status == 'available':
            try:
                self.session.accept(staged)
            except Exception:
                self.failed = True
                raise
        self._committed_observation = deepcopy(cycle['nextObservation'])
        self._committed_truth = deepcopy(cycle['truthAfter'])
        self._array_cache = array_cache.maps[0]
        return self.cycles-1

    def close(self, reason):
        if self.file.closed:
            self._array_cache.clear()
            return
        if self.failed:
            reason = 'controller_error'
        next_index = dict(self.index, status=reason, endReason=reason)
        try:
            atomic_json(self.directory / 'records.json', next_index)
            self.index = next_index
        finally:
            self.file.close()
            self._array_cache.clear()


def read_transitions(directory):
    directory = Path(directory)
    index = json.loads((directory / 'records.json').read_text())
    validate_protocol(index, 'sceneagent-control-transitions-v6', native=False)
    from runtime.scene import validate_recording
    validate_recording(index.get('recording'))
    if any(type(index.get(key)) is not int or index[key] < 0 for key in ('committedBytes', 'committedCycles', 'physicalSteps')):
        raise ValueError('invalid committed record boundary')
    initial = index.get('initialSnapshot')
    if initial is None or index.get('initialSnapshotDigest') != _physical_digest(initial):
        raise ValueError('initial snapshot is missing or its digest differs')
    reset = index.get('initialRubric', {})
    if reset.get('facts') != reset_facts(initial) or reset.get('factsDigest') != _physical_digest(reset['facts']):
        raise ValueError('reset rubric facts differ from the unique initial snapshot')
    raw = (directory / 'transitions.jsonl').read_bytes()
    prefix = raw[:index['committedBytes']]
    if len(prefix) != index['committedBytes'] or (prefix and not prefix.endswith(b'\n')):
        raise ValueError('committed transition prefix is missing or truncated')
    rows = [json.loads(line) for line in prefix.splitlines()]
    array_cache = {}
    validate_streams(initial['observation'], index['io'], directory=directory, _array_cache=array_cache)
    validate_streams(initial['truth'], index['io'], truth=True, directory=directory, _array_cache=array_cache)
    _validate_boundary(initial['observation'], initial['truth'])
    if len(rows) != index['committedCycles']:
        raise ValueError('committed cycle count mismatch')
    if sum(row['physicalSteps'] for row in rows) != index['physicalSteps']:
        raise ValueError('committed physical step total mismatch')
    ended = bool(reset['result']['terminated'] or reset['scoreStatus'] == 'unavailable')
    previous_observation, previous_truth = initial['observation'], initial['truth']
    previous_cache = array_cache
    for tick, row in enumerate(rows):
        array_cache = ChainMap({}, previous_cache)
        _validate_cycle(row, index['io'], directory, _array_cache=array_cache)
        if row['cycleId'] != tick:
            raise ValueError('committed cycle identity differs')
        if ended:
            raise ValueError('control cycles exist after episode termination')
        if row['observation'] != previous_observation or row['truthBefore'] != previous_truth:
            raise ValueError('control cycle initial/previous boundary facts differ')
        facts = cycle_facts(row)
        if row.get('facts') != facts or row.get('factsDigest') != _physical_digest(facts):
            raise ValueError('committed rubric facts differ from actual control facts')
        if row['terminated'] != row['rubricResult']['terminated']:
            raise ValueError('generated termination differs from committed result')
        ended = bool(row['terminated'] or row['truncated'])
        previous_observation, previous_truth = row['nextObservation'], row['truthAfter']
        previous_cache = array_cache.maps[0]
    last = rows[-1] if rows else None
    if index.get('lastResult') != (last['rubricResult'] if last else reset['result']):
        raise ValueError('record index last result differs from the committed prefix')
    if bool(index.get('terminated')) != bool(last['terminated'] if last else reset['result']['terminated']):
        raise ValueError('record index termination differs from committed facts')
    if bool(index.get('truncated')) != bool(last['truncated'] if last else reset['scoreStatus'] == 'unavailable'):
        raise ValueError('record index truncation differs from committed facts')
    return rows, index, len(raw) > len(prefix)


def execution_counts(directory, total_executed_steps, *, _transitions=None):
    """Physical execution can have a diagnostic tail beyond the committed rows."""
    _, index, tail = read_transitions(directory) if _transitions is None else _transitions
    if type(total_executed_steps) is not int or total_executed_steps < index['physicalSteps']:
        raise ValueError('executed physical steps are smaller than the committed prefix')
    return {'controlCycles': index['committedCycles'], 'physicalSteps': index['physicalSteps'],
            'committedPhysicalSteps': index['physicalSteps'], 'totalExecutedPhysicalSteps': total_executed_steps,
            'uncommittedPhysicalSteps': total_executed_steps-index['physicalSteps'], 'uncommittedRecordTail': tail}


def validate_camera_frames(observation, recording, frame_index=None, camera_ids=None, *, physics_rate_hz):
    if observation.get('cameraFrame') is not None:
        raise ValueError('legacy cameraFrame is not a native camera reference')
    cameras = {camera['cameraId']: camera for camera in recording['cameras']}
    references = observation.get('cameraFrames', {})
    if not isinstance(references, dict) or set(references)-set(cameras):
        raise ValueError('observation references an undeclared camera')
    if camera_ids is not None and set(references)-set(camera_ids):
        raise ValueError('observation includes an unauthorized camera')
    for id, frame in references.items():
        if not isinstance(frame, dict) or frame.get('cameraId') != id:
            raise ValueError('camera frame identity differs from its reference')
        if frame.get('status') == 'unavailable':
            if not isinstance(frame.get('reason'), str) or not frame['reason']:
                raise ValueError('unavailable camera requires its actual failure reason')
            continue
        for key in ('index', 'frameIndex', 'sampleId', 'physicsStep'):
            if type(frame.get(key)) is not int or frame[key] < 0:
                raise ValueError('camera frame index or physical sample identity is invalid')
        if frame['sampleId'] != frame['physicsStep']:
            raise ValueError('camera sampleId differs from its actual physical step')
        if (type(frame.get('stageId')) is not type(observation.get('stageId'))
                or frame.get('stageId') != observation.get('stageId')):
            raise ValueError('camera frame belongs to another stage')
        if (type(frame.get('timeOrigin')) not in (int, float) or not math.isfinite(frame['timeOrigin'])
                or frame['timeOrigin'] != observation['timeOrigin']):
            raise ValueError('camera physical time origin differs from its observation')
        for key in ('time', 'simulationTime'):
            if type(frame.get(key)) not in (int, float) or not math.isfinite(frame[key]):
                raise ValueError('nonfinite camera physical clock')
        if (abs(frame['simulationTime']-observation['timeOrigin']-frame['time']) > CAMERA_TIME_TOLERANCE
                or abs(frame['time']-frame['physicsStep']/physics_rate_hz) > CAMERA_TIME_TOLERANCE
                or frame['physicsStep'] > observation['physicsStep']
                or frame['time'] > observation['time']+CAMERA_TIME_TOLERANCE
                or frame['simulationTime'] > observation['simulationTime']+CAMERA_TIME_TOLERANCE):
            raise ValueError('future camera frame or physical clock/origin mismatch')
        path, sha = frame.get('path'), frame.get('sha256')
        if (not isinstance(path, str) or not path or PurePosixPath(path).is_absolute()
                or '..' in PurePosixPath(path).parts or str(PurePosixPath(path)) != path
                or not isinstance(sha, str) or len(sha) != 64 or any(char not in '0123456789abcdef' for char in sha)):
            raise ValueError('camera frame requires a scoped pixel path and digest')
        _physical_matrix(frame.get('worldFromCamera'), rigid=True)
        intrinsics = frame.get('intrinsics', {})
        if (not isinstance(intrinsics, dict) or set(intrinsics) != {'fx', 'fy', 'cx', 'cy'}
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in intrinsics.values())
                or intrinsics['fx'] <= 0 or intrinsics['fy'] <= 0):
            raise ValueError('camera frame requires its actual finite calibration')
        if frame_index is not None:
            original = frame_index.get(frame['index'])
            if original is None or any(original.get(key) != value for key, value in frame.items()):
                raise ValueError('camera reference differs from the unique trajectory frame index')
    return references


def project_annotations(rows, task, rubric):
    """Interpret generated labels only; source facts and task success remain unchanged."""
    result, active = [], {}
    for row in rows:
        details = row['rubricResult'].get('details')
        annotations = details.get('annotations', []) if isinstance(details, dict) else []
        if not isinstance(annotations, list):
            raise ValueError('generated annotations must be a list')
        seen, current = set(), {}
        for annotation in annotations:
            if not isinstance(task, dict) or not isinstance(task.get('bindings'), dict):
                raise ValueError('generated annotations require frozen task bindings')
            if (not isinstance(annotation, dict) or set(annotation) != {'id', 'text', 'bindings'}
                    or not isinstance(annotation['id'], str) or not annotation['id']
                    or not isinstance(annotation['text'], str) or not annotation['text']
                    or not isinstance(annotation['bindings'], list)
                    or any(not isinstance(role, str) or role not in task['bindings'] for role in annotation['bindings'])
                    or len(set(annotation['bindings'])) != len(annotation['bindings']) or annotation['id'] in seen):
                raise ValueError('generated annotation has invalid identity, text or binding roles')
            seen.add(annotation['id'])
            nodes = []
            for role in annotation['bindings']:
                reference = task['bindings'][role]
                id = reference if isinstance(reference, str) else reference.get('objectId', reference.get('frame'))
                if not isinstance(id, str) or not id:
                    raise ValueError('generated annotation binding lacks a frozen nodeId')
                nodes.append(id)
            key = json.dumps(annotation, sort_keys=True)
            prior = active.get(key)
            if prior is None or prior['cycleEnd'] != row['cycleId']:
                prior = {'kind': 'generated_interpretation', **deepcopy(annotation), 'nodeIds': sorted(set(nodes)),
                         'cycleStart': row['cycleId'], 'cycleEnd': row['cycleId'],
                         'timeStart': row['timeStart'], 'timeEnd': row['timeStart'],
                         'source': {'taskId': task['id'], 'rubric': deepcopy(rubric)}, 'factRefs': []}
                result.append(prior)
            prior.update(cycleEnd=row['cycleId']+1, timeEnd=row['timeEnd'])
            prior['factRefs'].append({'cycleId': row['cycleId'], 'factsDigest': row['factsDigest']})
            current[key] = prior
        active = current
    return result


def load_batch(directory, io=None, *, _transitions=None):
    """Read structured paired samples. Qualification needs the actual frozen I/O."""
    rows, index, tail = read_transitions(directory) if _transitions is None else _transitions
    if io is not None and validate_io(io) != index['io']:
        raise ValueError('record I/O differs from the frozen environment contract')
    if not rows:raise ValueError('empty demonstration has no training samples')
    rate = index['io']['clock']['physicsRateHz']
    for row in rows:
        for value in (row['observation'], row['nextObservation']):
            camera_ids = None
            if index.get('task') is not None:
                permission = index['task'].get('measurements', {}).get('camera', {})
                camera_ids = permission.get('cameraIds', []) if permission.get('visibleToPolicy') else []
            validate_camera_frames(value, index['recording'], camera_ids=camera_ids, physics_rate_hz=rate)
        for value in (row['truthBefore'], row['truthAfter']):
            validate_camera_frames(value, index['recording'], physics_rate_hz=rate)
    return {'observations':[row['observation'] for row in rows],
            'actions':[row['executedAction'] for row in rows],
            'nextObservations':[row['nextObservation'] for row in rows],
            'structuredObservations':[row['observation'] for row in rows],
            'structuredNextObservations':[row['nextObservation'] for row in rows],
            'controlCycles':len(rows), 'physicalSteps':index['physicalSteps'],
            'status':index['status'], 'uncommittedTail':tail, 'transitions':rows,
            'terminated':index['terminated'], 'truncated':index['truncated'], 'lastResult':index['lastResult'],
            'rubric':index['rubric'], 'initialSnapshot':index['initialSnapshot'], 'io':index['io'],
            'recording':index['recording'], 'task':index.get('task'),
            'semanticAnnotations':project_annotations(rows,index.get('task'),index['rubric'])}
