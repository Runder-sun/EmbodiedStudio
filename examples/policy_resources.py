"""Versioned policy resources and diagnostics for an unchanged frozen evaluator.

The original evaluator, worker, physics, rubric, observation and action protocol
remain intact. Only the explicitly bound candidate controller gains a read-only
resource mount and a separate output directory. The supplementary execution
receipt and candidate diagnostics are separate from native task scores.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


def _plain(root, name):
    relative = Path(name)
    if not name or relative.is_absolute() or '..' in relative.parts or relative.as_posix() != name:
        raise ValueError('resource path must be plain and relative')
    path = root/relative
    if any(part.is_symlink() for part in [path, *path.parents] if part != root and part.is_relative_to(root)):
        raise ValueError('resource links are not allowed')
    if not path.is_file(): raise ValueError('resource file is absent')
    return path


def _digest(path):
    value = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode): raise ValueError('resource must be a regular file')
        for block in iter(lambda: stream.read(1024*1024), b''): value.update(block)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('resource changed while reading')
    return {'bytes':before.st_size, 'sha256':value.hexdigest()}


def _tree(root, *, allow_empty=False):
    if root.is_symlink() or not root.is_dir(): raise ValueError('resource root must be a plain directory')
    files = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in names:
            if (Path(directory)/name).is_symlink(): raise ValueError('resource directory links are not allowed')
        for name in filenames:
            path = Path(directory)/name
            relative = path.relative_to(root).as_posix()
            files[relative] = _digest(_plain(root, relative))
    if not files and not allow_empty: raise ValueError('empty resource closure')
    return dict(sorted(files.items()))


def verify(manifest_path):
    manifest_path = Path(manifest_path).absolute()
    if manifest_path.is_symlink() or manifest_path.parent.is_symlink(): raise ValueError('resource manifest links are not allowed')
    manifest = json.loads(manifest_path.read_text())
    if set(manifest) != {'schema','files'} or manifest['schema'] != 'sceneagent-policy-resources-v1':
        raise ValueError('unsupported resource manifest')
    if _tree(manifest_path.parent/'files') != manifest['files']:
        raise ValueError('resource closure differs from declared bytes and digests')
    return manifest


def freeze(source, destination):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if destination.exists(): raise FileExistsError(destination)
    files = _tree(source)
    if destination.is_relative_to(source): raise ValueError('resource freeze must be outside its source')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.policy-resources-', dir=destination.parent))
    try:
        for name, expected in files.items():
            target = temporary/'files'/name; target.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(_plain(source, name), os.O_RDONLY | os.O_NOFOLLOW), 'rb') as incoming, target.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024*1024)
            if _digest(target) != expected: raise ValueError('source changed during resource freeze')
            target.chmod(0o444)
        manifest = {'schema':'sceneagent-policy-resources-v1','files':files}
        (temporary/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True)+'\n')
        verify(temporary/'manifest.json')
        temporary.rename(destination)
        return destination/'manifest.json'
    finally:
        if temporary.exists(): shutil.rmtree(temporary)


def socket_identity(value):
    path = Path(value).absolute()
    try:
        info = path.lstat()
        parent = path.parent.stat()
    except OSError as error:
        raise ValueError('policy socket is absent') from error
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
            or parent.st_uid != os.geteuid() or parent.st_mode & 0o077):
        raise ValueError('policy socket must belong to this user in a private directory')
    return {'path':str(path.resolve()), 'device':info.st_dev, 'inode':info.st_ino}


def mount_arguments(arguments, context, request):
    if not arguments or arguments[-1] != '--controller': return arguments
    if (arguments[-3:] != [context['python'], context['worker'], '--controller']
            or any(flag not in arguments[:-3] for flag in ('--unshare-all','--die-with-parent','--new-session','--clearenv'))):
        raise ValueError('policy mount requires the original isolated frozen controller')
    if not isinstance(request, dict) or any(request.get(key) != value for key, value in context['binding'].items()):
        raise ValueError('policy resource evaluation binding differs')
    if request.get('operation') != 'evaluate': raise ValueError('policy resources require independent evaluation')
    if _digest(Path(context['worker']))['sha256'] != context['workerSha256']:
        raise ValueError('frozen worker changed')
    manifest = Path(context['resourceManifest'])
    if _digest(manifest)['sha256'] != context['resourceManifestSha256']:
        raise ValueError('resource manifest changed')
    mounts = ['--ro-bind', str(manifest.parent/'files'), '/policy-resources',
              '--setenv', 'SCENEAGENT_POLICY_RESOURCE_ROOT', '/policy-resources']
    if endpoint := context.get('policySocket'):
        if socket_identity(endpoint['path']) != endpoint:
            raise ValueError('policy socket changed')
        mounts += ['--ro-bind', endpoint['path'], '/policy-inference.sock',
                   '--setenv', 'SCENEAGENT_POLICY_SOCKET', '/policy-inference.sock']
    if context.get('policyOutput'):
        output = Path(context['policyOutput'])
        if output.is_symlink() or not output.is_dir(): raise ValueError('policy output must be a plain execution directory')
        mounts += ['--bind', str(output), '/policy-output',
                   '--setenv', 'SCENEAGENT_POLICY_OUTPUT_ROOT', '/policy-output']
    return [*arguments[:-3], *mounts, *arguments[-3:]]


def _bubblewrap():
    context_path = Path(os.environ['SCENEAGENT_POLICY_RESOURCE_CONTEXT'])
    context = json.loads(context_path.read_text())
    if _digest(context_path)['sha256'] != os.environ['SCENEAGENT_POLICY_RESOURCE_CONTEXT_SHA256']:
        raise ValueError('resource execution context changed')
    if _digest(Path(__file__))['sha256'] != context['adapterSha256']:
        raise ValueError('resource adapter changed')
    arguments = sys.argv[1:]
    request = None
    if arguments and arguments[-1] == '--controller':
        parent = (Path('/proc')/str(os.getppid())/'cmdline').read_bytes().decode().split('\0')
        if len(parent) < 4 or parent[0:3] != [context['python'],context['worker'],'--kit-worker']:
            raise ValueError('policy mount requires the actual frozen Kit worker parent')
        request = json.loads((Path(parent[3])/'worker-request.json').read_text())
    mounted = mount_arguments(arguments, context, request)
    if mounted != arguments:
        receipt = {key:context[key] for key in ('binding','workerSha256','adapterSha256','resourceManifestSha256')}
        receipt.update(readOnly=True, networkIsolationPreserved=True, mount='/policy-resources',
                       policyOutput=context.get('policyOutput'), policyOutputMount='/policy-output',
                       originalArgumentsSha256=hashlib.sha256(json.dumps(arguments).encode()).hexdigest())
        if context.get('policySocket'):
            receipt['policySocket'] = context['policySocket']
        with (context_path.parent/'controller-mount.json').open('x') as stream: json.dump(receipt, stream)
    os.execv(context['bubblewrap'], [context['bubblewrap'], *mounted])


def evaluate(args):
    resources = Path(args.resources).resolve()
    endpoint = socket_identity(args.policy_socket) if getattr(args, 'policy_socket', None) else None
    verify(resources)
    manifest_path = Path(args.manifest).resolve(); manifest = json.loads(manifest_path.read_text())
    if manifest.get('runtimeExecution', {}).get('python') != sys.executable or manifest['runtimeExecution'].get('prefix') != sys.prefix:
        raise ValueError('use the frozen environment interpreter and dependency prefix')
    root = Path(args.root).resolve() if args.root else manifest_path.parents[4]
    snapshot = root/manifest['runtimeSnapshotRef']; worker = snapshot/'runtime/worker.py'
    if not snapshot.resolve().is_relative_to(root/'runtime') or worker.is_symlink():
        raise ValueError('frozen worker leaves its declared runtime root')
    candidate = Path(args.candidate).resolve()
    names = [candidate.name, *args.helper]
    if len(set(names)) != len(names): raise ValueError('duplicate candidate source')
    source_files = {name:_plain(candidate.parent,name).read_text() for name in names}
    selection = {'sourceRef':candidate.name, 'helperRefs':args.helper, 'sourceFiles':source_files,
                 'sourceDigests':{name:hashlib.sha256(value.encode()).hexdigest() for name,value in source_files.items()}}
    binding = {'operation':'evaluate','root':str(root),'projectId':manifest['projectId'],'revision':manifest['revision'],
               'taskId':args.task,'candidate':selection}
    evaluator = Path(args.evaluator).resolve(); adapter = Path(__file__).resolve()
    bubblewrap = shutil.which('bwrap')
    if not bubblewrap or Path(bubblewrap).read_bytes()[:2] == b'#!':
        raise ValueError('an original native bubblewrap binary is required')
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=False)
    policy_output = output/'policy-output'; policy_output.mkdir()
    context = dict(binding=binding, python=sys.executable, worker=str(worker), workerSha256=_digest(worker)['sha256'],
                   resourceManifest=str(resources), resourceManifestSha256=_digest(resources)['sha256'],
                   adapterSha256=_digest(adapter)['sha256'], evaluatorSha256=_digest(evaluator)['sha256'],
                   environmentManifestSha256=_digest(manifest_path)['sha256'], bubblewrap=bubblewrap,
                   policyOutput=str(policy_output))
    if endpoint:
        context['policySocket'] = endpoint
    context_path = output/'context.json'; context_path.write_text(json.dumps(context, ensure_ascii=False)+'\n')
    launcher = output/'bwrap'
    launcher.write_text('#!'+sys.executable+'\nimport runpy\nrunpy.run_path('+repr(str(adapter))+',run_name="policy_resource_adapter")["_bubblewrap"]()\n')
    launcher.chmod(0o700)
    environment = {**os.environ, 'PATH':str(output)+os.pathsep+os.environ.get('PATH','/usr/bin:/bin'),
                   'SCENEAGENT_POLICY_RESOURCE_CONTEXT':str(context_path),
                   'SCENEAGENT_POLICY_RESOURCE_CONTEXT_SHA256':_digest(context_path)['sha256']}
    command = [sys.executable, str(evaluator), str(manifest_path), str(candidate), '--root', str(root),
               '--task', args.task, '--gpu-index', str(args.gpu_index), '--gpu-uuid', args.gpu_uuid,
               '--max-seconds', str(args.max_seconds), '--worker-timeout-seconds', str(args.worker_timeout_seconds)]
    for name in args.helper: command += ['--helper',name]
    if args.robot_setup_ref: command += ['--robot-setup-ref',args.robot_setup_ref]
    report = {'schema':'sceneagent-policy-resource-evaluation-v1','passed':False,'context':context,'nativeResult':None}
    try:
        with (output/'native-stdout.json').open('wb') as stdout, (output/'native-stderr.log').open('wb') as stderr:
            result = subprocess.run(command, env=environment, stdout=stdout, stderr=stderr)
        report['policyOutput'] = {'root':str(policy_output),'files':_tree(policy_output,allow_empty=True),
                                 'trust':'candidate-authored diagnostic data; not native observations or task scores'}
        if result.returncode: raise RuntimeError('frozen evaluation failed; see native-stderr.log')
        verify(resources)
        for path, expected in [(worker,context['workerSha256']), (adapter,context['adapterSha256']),
                               (evaluator,context['evaluatorSha256']), (manifest_path,context['environmentManifestSha256']),
                               (resources,context['resourceManifestSha256'])]:
            if _digest(path)['sha256'] != expected: raise ValueError('evaluation input changed during execution')
        mount = json.loads((output/'controller-mount.json').read_text())
        if mount['binding'] != binding or mount['resourceManifestSha256'] != context['resourceManifestSha256']:
            raise ValueError('actual controller resource receipt differs')
        if mount.get('policySocket') != endpoint:
            raise ValueError('actual controller policy socket receipt differs')
        report.update(passed=True, nativeResult=json.loads((output/'native-stdout.json').read_text()), mount=mount)
        return report
    except BaseException as error:
        report['error'] = type(error).__name__+': '+str(error)
        raise
    finally:
        (output/'result.json').write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)+'\n')


def main():
    if Path(sys.argv[0]).name == 'bwrap': return _bubblewrap()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    prepare = commands.add_parser('freeze'); prepare.add_argument('source'); prepare.add_argument('destination')
    run = commands.add_parser('evaluate')
    for name in ('resources','evaluator','manifest','candidate','task','output','gpu-uuid'): run.add_argument('--'+name,required=True)
    run.add_argument('--root'); run.add_argument('--helper', action='append', default=[])
    run.add_argument('--gpu-index', type=int, required=True)
    run.add_argument('--max-seconds', type=float, default=120)
    run.add_argument('--worker-timeout-seconds', type=float, default=3600)
    run.add_argument('--robot-setup-ref')
    run.add_argument('--policy-socket')
    args = parser.parse_args()
    if args.operation == 'freeze':
        path = freeze(args.source,args.destination)
        print(json.dumps({'manifest':str(path),'sha256':_digest(path)['sha256']}))
    else: print(json.dumps(evaluate(args),ensure_ascii=False,allow_nan=False))


if __name__ == '__main__': main()
