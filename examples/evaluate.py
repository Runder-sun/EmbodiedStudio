"""Evaluate a separate frozen candidate against a delivered environment manifest."""
from pathlib import Path
import argparse
import hashlib
import json
import math
import sys
import os
import subprocess
import tempfile



def evaluate(manifest_path, candidate_path, task_id, *, helpers=(), root=None, seed=None,
             max_seconds=120, rubric_seconds=30, device=None, worker_timeout_seconds=None, robot_setup_ref=None):
    manifest_path, candidate_path = Path(manifest_path).resolve(), Path(candidate_path).absolute()
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema') != 'sceneagent-build-v6':
        raise ValueError('This entry requires v6; use the preserved interpreter and frozen worker for an older package.')
    execution = manifest.get('runtimeExecution', {})
    if execution.get('python') != sys.executable or execution.get('prefix') != sys.prefix:
        raise ValueError('Use this package’s declared Python interpreter and complete dependency prefix.')
    if worker_timeout_seconds is not None:
        if type(worker_timeout_seconds) not in (int, float) or not math.isfinite(worker_timeout_seconds) or worker_timeout_seconds <= 0:
            raise ValueError('workerTimeoutSeconds must be positive finite seconds.')
        if execution.get('workerTimeoutParameter') != 'workerTimeoutSeconds':
            raise ValueError('This frozen worker does not declare workerTimeoutSeconds support; preserve it or rebuild with a compatible runtime.')
    if device is None:
        raise ValueError('Standalone evaluation requires an explicitly assigned GPU index and UUID.')
    base = candidate_path.parent.resolve()
    files = {}
    names = [candidate_path.name, *helpers]
    for index, name in enumerate(names):
        relative = Path(name)
        path = base/relative
        if (relative.is_absolute() or '..' in relative.parts
                or not (relative.suffix == '.py' or index > 0 and name.endswith('.robot.json'))
                or path.is_symlink() or not path.resolve().is_relative_to(base)
                or any(parent.is_symlink() for parent in path.parents if parent != base and parent.is_relative_to(base))):
            raise ValueError('candidate requires a plain relative Python entry and Python or public robot profile helpers')
        files[name] = path.read_text()
    if len(files) != len(names):
        raise ValueError('duplicate candidate helper')
    data_root = Path(root).resolve() if root else manifest_path.parents[4]
    payload = {'operation': 'evaluate', 'root': str(data_root), 'projectId': manifest['projectId'], 'device': device,
               'revision': manifest['revision'], 'taskId': task_id,
               'seed': manifest['seed'] if seed is None else seed,
               'maxSeconds': max_seconds, 'rubricSeconds': rubric_seconds,
               'candidate': {'sourceRef': candidate_path.name, 'helperRefs': list(helpers), 'sourceFiles': files,
                             'sourceDigests': {name: hashlib.sha256(source.encode()).hexdigest() for name, source in files.items()}}}
    if worker_timeout_seconds is not None:
        payload['workerTimeoutSeconds'] = worker_timeout_seconds
    if robot_setup_ref is not None:
        if manifest.get('capabilities', {}).get('robotSetup') is not True:
            raise ValueError('This frozen worker does not support robot installation references.')
        payload['robotSetupRef'] = robot_setup_ref
    reference = Path(manifest['runtimeSnapshotRef'])
    snapshot = (data_root/reference).resolve()
    worker = snapshot/'runtime/worker.py'
    if (reference.is_absolute() or '..' in reference.parts or not snapshot.is_relative_to(data_root/'runtime')
            or worker.is_symlink() or not worker.is_file()):
        raise ValueError('The frozen runtime entry is missing or outside its declared data root.')
    environment = {**os.environ, 'PYTHONPATH': str(snapshot), 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    result = None
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([sys.executable, str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=errors, env=environment)
        try:
            process.stdin.write(json.dumps(payload, allow_nan=False).encode())
            process.stdin.close()
            while True:
                line = process.stdout.readline(8*1024*1024+1)
                if not line:
                    break
                if len(line) > 8*1024*1024:
                    raise ValueError('Frozen evaluator response exceeds its byte bound.')
                event = json.loads(line)
                if event.get('type') == 'result':
                    if result is not None:
                        raise ValueError('Frozen evaluator returned multiple results.')
                    result = event['result']
                elif event.get('type') == 'error':
                    raise RuntimeError(event.get('error', 'Frozen evaluator failed.'))
            code = process.wait()
            if code or result is None:
                errors.seek(0, 2)
                errors.seek(max(0, errors.tell()-4000))
                raise RuntimeError(errors.read().decode(errors='replace') or 'Frozen evaluator did not complete.')
            return result
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if not process.stdin.closed:
                process.stdin.close()
            process.stdout.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    parser.add_argument('candidate')
    parser.add_argument('--task', required=True)
    parser.add_argument('--helper', action='append', default=[])
    parser.add_argument('--root')
    parser.add_argument('--seed', type=int)
    parser.add_argument('--gpu-index', type=int, required=True)
    parser.add_argument('--gpu-uuid', required=True)
    parser.add_argument('--max-seconds', type=float, default=120)
    parser.add_argument('--rubric-seconds', type=float, default=30)
    parser.add_argument('--worker-timeout-seconds', type=float)
    parser.add_argument('--robot-setup-ref')
    args = parser.parse_args()
    print(json.dumps(evaluate(args.manifest, args.candidate, args.task, helpers=args.helper, root=args.root,
        seed=args.seed, max_seconds=args.max_seconds, rubric_seconds=args.rubric_seconds,
        worker_timeout_seconds=args.worker_timeout_seconds, robot_setup_ref=args.robot_setup_ref,
        device={'index':args.gpu_index, 'uuid':args.gpu_uuid}), ensure_ascii=False, allow_nan=False))
