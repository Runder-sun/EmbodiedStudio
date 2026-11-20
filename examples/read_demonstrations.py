"""Load one committed episode into numeric/action batches and pixel references.

Qualification requires the episode's complete frozen v6 interpreter prefix and
dependencies. The yielded values and media references can train other consumers;
reading them is not an independent revalidation of simulator physics on plain CPU.
"""
from pathlib import Path
from copy import deepcopy
import json
import hashlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.records import load_batch, validate_protocol, validate_publication, validate_camera_frames, has_control_ports


def _digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def _load(directory):
    directory = Path(directory).resolve()
    root = directory.parents[3]
    manifest = json.loads((directory / 'manifest.json').read_text())
    validate_protocol(manifest, 'sceneagent-episode-v6', native=False)
    validate_publication(manifest, root)
    if (manifest.get('status') != 'completed' or manifest.get('executionStatus') not in ('completed', 'truncated')
            or manifest.get('purpose') != 'demonstration' or manifest.get('controllerKind') != 'expert'):
        raise ValueError('episode is diagnostic data, not complete generated behavior data')
    expected = manifest.get('artifactDigests', {})
    if not {'records', 'transitions', 'trajectory', 'annotations'}.issubset(expected):
        raise ValueError('demonstration lacks committed record digests')
    checked_digests = {}
    for label, digest in expected.items():
        path = (root / manifest['artifacts'][label]).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise ValueError('demonstration artifact digest mismatch')
        if path not in checked_digests:
            checked_digests[path] = _digest(path)
        if checked_digests[path] != digest:
            raise ValueError('demonstration artifact digest mismatch')
    from runtime.scene import Task
    from runtime.worker import frozen, _verify_media, recorded_behavior, robot_environment
    project, source, build, world = frozen(root, {'projectId':manifest['projectId'], 'revision':manifest['revision']})
    source, build, world, _ = robot_environment(root, project, source, build, world, manifest)
    if any(manifest.get(key) != build.get('_robotSetup', {}).get(key) for key in ('robotSetupRef','robotSetupSha256')):
        raise ValueError('demonstration robot installation differs from its frozen input')
    if 'behaviorRef' in manifest:
        selected, _ = recorded_behavior(root, root/'projects'/manifest['projectId'], manifest)
        if any(manifest.get(field) != selected[key] for field, key in (
                ('controllerSource','sourceRef'),('controllerDigest','sourceDigest'),('controllerHelperDigests','helperDigests'))):
            raise ValueError('demonstration differs from its independent behavior source')
    task = next((Task(**row) for row in build['tasks'] if row['id'] == manifest['taskId']), None)
    if task is None or manifest.get('manifestRef') != build['artifacts']['manifest']:
        raise ValueError('demonstration does not belong to its frozen environment task')
    if manifest.get('io') != build.get('io'):
        raise ValueError('demonstration I/O differs from its frozen environment')
    if not has_control_ports(build['io']) or manifest.get('executionKind') != 'actuator_physics_native':
        raise ValueError('robot demonstration requires actual frozen control ports and actuator execution')
    batch = load_batch(directory, io=build['io'])
    if (not isinstance(batch['task'], dict) or batch['task'].get('id') != manifest['taskId']
            or not isinstance(batch['task'].get('instruction'), str) or not isinstance(batch['task'].get('bindings'), dict)):
        raise ValueError('demonstration lacks its frozen task semantics')
    if any(batch[key] != manifest.get(key) for key in ('io', 'recording')):
        raise ValueError('demonstration specification differs from its published contract')
    if (batch['recording'] != task.recording
            or any(batch['task'][key] != getattr(task, key) for key in batch['task'])):
        raise ValueError('demonstration task or sensor specification differs from the frozen build')
    if (batch['rubric'] != manifest.get('rubric') or batch['lastResult'] != manifest.get('result')
            or batch['uncommittedTail'] or any(row['scoreStatus'] != 'available' for row in batch['transitions'])):
        raise ValueError('demonstration generated result or rubric differs from the published record')
    eligible = manifest['executionStatus'] == 'completed' and batch['lastResult']['success'] is True
    if manifest.get('demonstrationEligible') is not eligible:
        raise ValueError('successful demonstration label differs from actual behavior result')
    trajectory = json.loads((root/manifest['artifacts']['trajectory']).read_text())
    if (trajectory.get('schema') != 'sceneagent-studio-trajectory-v6'
            or trajectory.get('revision') != manifest['revision']
            or any(trajectory.get('task', {}).get(key) != value for key, value in (batch['task'] or {}).items())):
        raise ValueError('demonstration trajectory differs from the frozen task or revision')
    frames = {frame['index']: frame for frame in trajectory['frames']}
    if len(frames) != len(trajectory['frames']):
        raise ValueError('duplicate camera frame index')
    _verify_media(directory, manifest, trajectory, root=root, world=world, task=task)
    for observation in batch['structuredObservations'] + batch['structuredNextObservations']:
        validate_camera_frames(observation, batch['recording'], frames, physics_rate_hz=batch['io']['clock']['physicsRateHz'])
    for row in batch['transitions']:
        for truth in (row.get('truthBefore'), row.get('truthAfter')):
            if isinstance(truth, dict) and 'cameraFrames' in truth:
                validate_camera_frames(truth, batch['recording'], frames, physics_rate_hz=batch['io']['clock']['physicsRateHz'])
    annotations = json.loads((root/manifest['artifacts']['annotations']).read_text())
    expected_annotations = {'schema': 'sceneagent-generated-annotations-v1', 'taskId': manifest['taskId'],
        'rubric': batch['rubric'], 'bindings': (batch['task'] or {}).get('bindings', {}),
        'annotations': batch['semanticAnnotations']}
    if annotations != expected_annotations:
        raise ValueError('demonstration annotations differ from the committed generated interpretation')
    return manifest, batch, trajectory


def consume(directory):
    manifest, batch, trajectory = _load(directory)
    report = {'passed': True, 'episodeId': manifest['episodeId'], 'revision': manifest['revision'],
              'taskId': manifest['taskId'], 'controlCycles': batch['controlCycles'],
              'physicalSteps': batch['physicalSteps'], 'io': batch['io'],
              'uncommittedTail': batch['uncommittedTail']}
    report.update(rubric=batch['rubric'], result=batch['lastResult'], demonstrationEligible=manifest['demonstrationEligible'],
                  terminated=batch['terminated'], truncated=batch['truncated'])
    if 'behaviorRef' in manifest: report['behaviorRef'] = manifest['behaviorRef']
    if 'robotSetupRef' in manifest:
        report.update({key:manifest[key] for key in ('robotSetupRef','robotSetupSha256')})
    report.update(task=batch['task'], media=manifest.get('media'),
                  mediaIndex={'artifactRef': manifest['artifacts']['trajectory'], 'frameCount': len(trajectory['frames'])},
                  semantics={'kind':'generated_interpretation', 'artifactRef':manifest['artifacts']['annotations'],
                             'annotationCount':len(batch['semanticAnnotations']), 'rubric':batch['rubric']})
    # Real paired samples are exposed, rather than just a list of artifact names.
    report['firstSample'] = {'observation': batch['observations'][0],
                            'action': batch['actions'][0],
                            'nextObservation': batch['nextObservations'][0]}
    return report


def samples(directory):
    """Yield qualified paired data and pixel references; never inline video or rubric private state."""
    manifest, batch, trajectory = _load(directory)
    frames = {frame['index']: frame for frame in trajectory['frames']}
    for row in batch['transitions']:
        details = row['rubricResult'].get('details')
        references = (row.get('truthBefore') or {}).get('cameraFrames', row['observation'].get('cameraFrames', {}))
        yield {'episodeId':manifest['episodeId'], 'revision':manifest['revision'], 'taskId':manifest['taskId'],
               **({key:manifest[key] for key in ('robotSetupRef','robotSetupSha256')} if 'robotSetupRef' in manifest else {}),
               'instruction':(batch['task'] or {}).get('instruction', ''),
               'demonstrationEligible':manifest['demonstrationEligible'], 'result':deepcopy(row['rubricResult']),
               'bindings':(batch['task'] or {}).get('bindings', {}), 'cycleId':row['cycleId'],
               'observation':row['observation'], 'action':row['executedAction'], 'nextObservation':row['nextObservation'],
               'policyCameraIds':list(row['observation'].get('cameraFrames', {})),
               'cameras':{id:frames[reference['index']] for id, reference in references.items()
                          if reference.get('status') != 'unavailable'},
               'annotations':{'kind':'generated_interpretation', 'rubric':batch['rubric'],
                              'factsDigest':row['factsDigest'],
                              'values':details.get('annotations', []) if isinstance(details, dict) else []}}


if __name__ == '__main__':
    print(json.dumps(consume(sys.argv[1]), ensure_ascii=False, allow_nan=False))
