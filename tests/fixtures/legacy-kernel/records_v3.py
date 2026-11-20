"""Committed control transitions; camera/video samples have an independent clock."""
from __future__ import annotations

import json
import hashlib
import math
from numbers import Real
import os
from pathlib import Path
import tempfile


ACTION_MODE = 'joint_targets_gripper_width_m'
JOINT_NAMES = [f'panda_joint{i}' for i in range(1, 8)] + ['panda_finger_joint1', 'panda_finger_joint2']
ACTION_SPEC = {'mode': ACTION_MODE, 'shape': [8], 'controlRateHz': 50,
               'jointOrder': JOINT_NAMES[:7] + ['gripper_total_width'],
               'units': ['rad'] * 7 + ['m'],
               'ranges': [[-2.8973, 2.8973], [-1.7628, 1.7628], [-2.8973, 2.8973],
                          [-3.0718, -.0698], [-2.8973, 2.8973], [-.0175, 3.7525],
                          [-2.8973, 2.8973], [0, .08]]}


def validate_action_spec(spec):
    if any(spec.get(key) != ACTION_SPEC[key] for key in ('mode', 'shape', 'jointOrder', 'units', 'controlRateHz')):
        raise ValueError('unsupported Isaac action protocol, units or joint order')
    ranges = spec.get('ranges', [])
    if len(ranges) != 8 or ranges[-1] != [0, .08] or any(
            len(pair) != 2 or not all(math.isfinite(value) for value in pair) or pair[0] >= pair[1]
            for pair in ranges):
        raise ValueError('action protocol requires frozen finite joint/gripper ranges')
    if any(abs(actual - expected) > .0002 for actual_pair, expected_pair in zip(ranges[:7], ACTION_SPEC['ranges'][:7])
           for actual, expected in zip(actual_pair, expected_pair)):
        raise ValueError('action ranges differ from the native Franka joint limits')
    return spec


def validate_action(action, spec):
    if len(action) != 8 or not all(isinstance(value, Real) and math.isfinite(value) for value in action):
        raise ValueError('action must contain seven finite joint targets and one gripper width')
    for index, (value, limits) in enumerate(zip(action, spec['ranges'])):
        if not limits[0] <= value <= limits[1]:
            name = 'gripper total width in meters' if index == 7 else spec['jointOrder'][index]
            raise ValueError(f'{name} outside frozen range')


def validate_protocol(manifest, schema):
    if manifest.get('schema') != schema or manifest.get('engine') != 'isaacsim' or manifest.get('physics') != 'physx':
        raise ValueError('unsupported simulation protocol; rebuild with Isaac v3')
    validate_action_spec(manifest['actionSpec'])
    if schema in ('sceneagent-build-v3', 'sceneagent-episode-v3'):
        spec = manifest.get('observationSpec', {})
        if spec.get('jointOrder') != JOINT_NAMES or spec.get('positionUnits') != ['rad']*7+['m']*2 or spec.get('velocityUnits') != ['rad/s']*7+['m/s']*2:
            raise ValueError('unsupported nine-joint observation protocol or units')


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


class Recorder:
    def __init__(self, directory, action_spec=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.action_spec = validate_action_spec(action_spec or ACTION_SPEC)
        self.file = (self.directory / 'transitions.jsonl').open('w')
        self.cycles, self.physical_steps = 0, 0
        self.index = {'schema': 'sceneagent-control-transitions-v3', 'engine': 'isaacsim', 'physics': 'physx',
                      'actionSpec': self.action_spec, 'physicsRateHz': 500, 'observationJointOrder': JOINT_NAMES, 'committedCycles': 0,
                      'committedBytes': 0, 'physicalSteps': 0, 'status': 'running'}
        atomic_json(self.directory / 'records.json', self.index)

    def commit(self, before, target_action, executed_action, after, physical_steps, events=(), truth_before=None, truth_after=None):
        validate_action(target_action, self.action_spec)
        validate_action(executed_action, self.action_spec)
        cycle = {'engine': 'isaacsim', 'physics': 'physx', 'cycleId': self.cycles, 'timeStart': before['time'], 'timeEnd': after['time'],
                 'observation': before, 'nextObservation': after,
                 'targetAction': [float(value) for value in target_action], 'executedAction': [float(value) for value in executed_action],
                 'noise': [float(b - a) for a, b in zip(target_action, executed_action)],
                 'physicalSteps': int(physical_steps), 'events': list(events),
                 'actionMode': ACTION_MODE, 'terminated': False, 'truncated': False,
                 'truthBefore': truth_before, 'truthAfter': truth_after}
        from runtime.jobs import measure
        with measure('recordWrite'):
            self.file.write(json.dumps(cycle, ensure_ascii=False, allow_nan=False) + '\n')
            self.file.flush()
            os.fsync(self.file.fileno())
        self.cycles += 1
        self.physical_steps += physical_steps
        self.index.update(committedCycles=self.cycles, committedBytes=self.file.tell(),
                          physicalSteps=self.physical_steps)
        # The index is the commit boundary. A crash before publication leaves a diagnostic tail.
        with measure('recordIndex'):
            atomic_json(self.directory / 'records.json', self.index)
        return self.cycles - 1

    def close(self, reason):
        if self.file.closed:
            return
        self.index.update(status=reason, terminated=reason == 'success',
                          truncated=reason in ('interrupted', 'budget_exhausted', 'controller_error'), endReason=reason)
        atomic_json(self.directory / 'records.json', self.index)
        self.file.close()


def read_transitions(directory):
    directory = Path(directory)
    index = json.loads((directory / 'records.json').read_text())
    validate_protocol(index, 'sceneagent-control-transitions-v3')
    if index.get('physicsRateHz') != 500:
        raise ValueError('unsupported recorded physics clock rate')
    if index.get('observationJointOrder') != JOINT_NAMES:
        raise ValueError('unsupported native observation joint order')
    if any(not isinstance(index.get(key), int) or index[key] < 0 for key in ('committedBytes', 'committedCycles', 'physicalSteps')):
        raise ValueError('invalid committed record boundary')
    raw = (directory / 'transitions.jsonl').read_bytes()
    prefix = raw[:index['committedBytes']]
    if len(prefix) != index['committedBytes'] or (prefix and not prefix.endswith(b'\n')):
        raise ValueError('committed transition prefix is missing or truncated')
    rows = [json.loads(line) for line in prefix.splitlines()]
    if len(rows) != index['committedCycles']:
        raise ValueError('committed cycle count mismatch')
    if sum(row['physicalSteps'] for row in rows) != index['physicalSteps']:
        raise ValueError('committed physical step total mismatch')
    return rows, index, len(raw) > len(prefix)


def execution_counts(directory, total_executed_steps):
    """Physical execution can have a diagnostic tail beyond the committed rows."""
    _, index, tail = read_transitions(directory)
    if type(total_executed_steps) is not int or total_executed_steps < index['physicalSteps']:
        raise ValueError('executed physical steps are smaller than the committed prefix')
    return {'controlCycles': index['committedCycles'], 'physicalSteps': index['physicalSteps'],
            'committedPhysicalSteps': index['physicalSteps'], 'totalExecutedPhysicalSteps': total_executed_steps,
            'uncommittedPhysicalSteps': total_executed_steps-index['physicalSteps'], 'uncommittedRecordTail': tail}


def load_batch(directory):
    import numpy as np
    rows, index, tail = read_transitions(directory)
    if not rows:
        raise ValueError('empty demonstration has no training samples')
    previous = None
    for tick, row in enumerate(rows):
        if row.get('engine') != 'isaacsim' or row.get('physics') != 'physx' or row.get('actionMode') != ACTION_MODE:
            raise ValueError('unsupported transition action protocol')
        validate_action(row['targetAction'], index['actionSpec'])
        validate_action(row['executedAction'], index['actionSpec'])
        before, after = row['observation'], row['nextObservation']
        if row['cycleId'] != tick or row['timeStart'] != before['time'] or row['timeEnd'] != after['time']:
            raise ValueError('cycle identity/time mismatch')
        if not isinstance(row['physicalSteps'], int) or after['time'] <= before['time'] or row['physicalSteps'] <= 0:
            raise ValueError('invalid control/physics clock')
        if previous is not None and abs(before['time'] - previous) > 1e-9:
            raise ValueError('control cycles have gaps or overlap')
        previous = after['time']
        elapsed = after['time'] - before['time']
        if not math.isfinite(elapsed) or abs(elapsed - row['physicalSteps'] / index['physicsRateHz']) > 1e-9:
            raise ValueError('control elapsed time differs from the recorded physics clock')
        if before.get('timeOrigin') != after.get('timeOrigin'):
            raise ValueError('control transition changed its physical time origin')
        if after.get('stageId') != before.get('stageId') or after.get('physicsStep', 0) - before.get('physicsStep', 0) != row['physicalSteps']:
            raise ValueError('control transition crosses stages or has wrong physical step count')
        for observation in (before, after):
            if len(observation['jointPosition']) != 9 or len(observation['jointVelocity']) != 9:
                raise ValueError('observation requires all nine actual native joints')
            camera = observation.get('cameraFrame')
            if camera and camera['time'] > observation['time'] + 1e-9:
                raise ValueError('future camera frame used as a pre-action observation')
            if 'simulationTime' not in observation or 'timeOrigin' not in observation or 'physicsStep' not in observation or 'stageId' not in observation:
                raise ValueError('observation lacks physical clock provenance')
            if not all(isinstance(observation[key], Real) and math.isfinite(observation[key]) for key in ('time', 'timeOrigin', 'simulationTime')):
                raise ValueError('nonfinite observation physical clock')
            if not isinstance(observation['physicsStep'], int):
                raise ValueError('physical step identity must be an integer')
            if abs(observation['simulationTime'] - observation['timeOrigin'] - observation['time']) > 1e-9:
                raise ValueError('observation physical clock/origin mismatch')
            if camera:
                if not all(isinstance(camera.get(key), Real) and math.isfinite(camera[key]) for key in ('time', 'simulationTime')):
                    raise ValueError('nonfinite camera physical clock')
                if camera.get('stageId') != observation.get('stageId'):
                    raise ValueError('camera frame belongs to another stage')
                if abs(camera.get('simulationTime', float('inf')) - observation['timeOrigin'] - camera['time']) > 1e-9:
                    raise ValueError('camera physical clock/origin mismatch')
                if camera.get('simulationTime', float('inf')) > observation['simulationTime'] + 1e-9:
                    raise ValueError('future physical camera clock')
                if camera.get('physicsStep', float('inf')) > observation['physicsStep']:
                    raise ValueError('camera frame physics step is newer than observation')
    def features(observation):
        return observation['jointPosition'] + observation['jointVelocity']
    observations = np.asarray([features(row['observation']) for row in rows], dtype=np.float64)
    actions = np.asarray([row['executedAction'] for row in rows], dtype=np.float64)
    next_observations = np.asarray([features(row['nextObservation']) for row in rows], dtype=np.float64)
    if not all(np.isfinite(value).all() for value in (observations, actions, next_observations)):
        raise ValueError('nonfinite demonstration values')
    return {'observations': observations, 'actions': actions, 'nextObservations': next_observations,
            'controlCycles': len(rows), 'physicalSteps': sum(row['physicalSteps'] for row in rows),
            'status': index['status'], 'uncommittedTail': tail, 'transitions': rows,
            'structuredObservations': [row['observation'] for row in rows],
            'structuredNextObservations': [row['nextObservation'] for row in rows]}
