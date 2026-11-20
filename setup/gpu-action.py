"""Run explicit setup checks under the existing GPU memory admission."""
import base64
import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


COMMIT = 'ebb71702f3f70e767f40fd8e050674af0288abe8'
REQUIRED = {'curobo','warp','torch', *['curobo.curobolib.'+name+'_cu' for name in ('geom','kinematics_fused','lbfgs_step','line_search','tensor_step')]}
NUMERICAL = {'actual_imports_and_native_extensions','actual_warp_cuda_initialization','cold_model_and_world_load','cold_warmup','warm_numerical_ik','warm_goalset_path','world_pose_update_changes_actual_collision_result','temporary_pose_metric_restores_after_exception','planning_payload_sphere_attach_detach_lifecycle'}


def digest(file):
    value=hashlib.sha256()
    with Path(file).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def build_manifest(q,directory):
    prefix=Path(q['prefix'])
    reports={phase:json.loads((directory/(phase+'-receipt.json')).read_text()) for phase in ('numerical','native')}
    probe_hash=hashlib.sha256(q['probeSource'].encode()).hexdigest()
    for phase,report in reports.items():
        required=NUMERICAL|({'isaac_same_process_startup','native_franka_actual_q_fk_and_tcp_calibration'} if phase=='native' else set())
        checks=report.get('checks',[])
        if (report.get('status')!='passed' or report.get('phase')!=phase or report.get('upstreamCommit')!=COMMIT
                or report.get('probeSourceSha256')!=probe_hash or report.get('identityFailure')
                or report.get('assignedGpu')!={'physicalIndex':q['device']['index'],'uuid':q['device']['uuid']}
                or not required<={row.get('name') for row in checks} or any(row.get('status')!='passed' for row in checks)):
            raise ValueError('incomplete, changed or failed actual '+phase+' verification receipt')
        identity=report['identity']
        if (identity.get('prefix')!=str(prefix) or identity.get('executable')!=str(prefix/'bin/python')
                or {row.get('uuid') for row in identity.get('actualProcessGpuDevices',[])}!={q['device']['uuid']}):
            raise ValueError('actual verification prefix/interpreter/device differs')
    native,numerical=reports['native']['identity'],reports['numerical']['identity']
    for key in ('prefix','executable','python','SOABI','distributions','torchABI','torchCUDA'):
        if key not in native or native[key]!=numerical.get(key):raise ValueError('environment changed across numerical/native phases: '+key)
    versions=native['distributions']
    if any(versions.get(name)!=version for name,version in {'isaacsim':'6.0.0.1','torch':'2.11.0+cu128','numpy':'2.3.1','warp-lang':None}.items()):raise ValueError('installed dependency family differs')
    if not reports['native'].get('identityCapturedBeforeSdkShutdown') or not native.get('contentHashesComputed') or not REQUIRED<=set(native.get('modules',{})):
        raise ValueError('full native identity omits required modules')
    binaries=native.get('mappedNativeLibraries',{})
    if not {native['modules'][name]['path'] for name in REQUIRED if name.startswith('curobo.curobolib.')}<=set(binaries):raise ValueError('required cuRobo native mapping omitted')
    for name in REQUIRED:
        actual,prior=native['modules'][name],numerical.get('modules',{}).get(name,{})
        if any(actual.get(key)!=prior.get(key) for key in ('path','bytes','device','inode','mtimeNs','ctimeNs')):raise ValueError('module changed across verification phases: '+name)
    def plain(file):
        file=Path(file)
        if not file.is_absolute() or file.is_symlink() or file.resolve()!=file or not file.is_relative_to(prefix) or not file.is_file():raise ValueError('selected source is outside ordinary verified prefix')
        return file
    for file,row in [*[(row['path'],row) for row in native['modules'].values()],*binaries.items()]:
        file=plain(file)
        if file.stat().st_size!=row['bytes'] or digest(file)!=row.get('sha256'):raise ValueError('actual loaded file content changed after verification')
    site=Path(native['modules']['curobo']['path']).parent.parent
    urls=list(site.glob('*curobo*.dist-info/direct_url.json'))
    if len(urls)!=1 or json.loads(urls[0].read_text()).get('vcs_info',{}).get('commit_id')!=COMMIT:raise ValueError('installed cuRobo commit provenance missing or different')
    sources={}
    for module in ('curobo','warp'):
        for file in Path(native['modules'][module]['path']).parent.rglob('*.py'):
            file=plain(file);sources[str(file)]={'bytes':file.stat().st_size,'sha256':digest(file)}
    file=plain(urls[0]);sources[str(file)]={'bytes':file.stat().st_size,'sha256':digest(file)}
    if not sources:raise ValueError('actual selected source identity missing')
    receipts={phase:{'path':str(directory/(phase+'-receipt.json')),'sha256':digest(directory/(phase+'-receipt.json'))} for phase in reports}
    return {'schema':'sceneagent-motion-dependencies-v1','status':'verified_new_prefix',
        'backend':{'name':'cuRobo','repository':'https://github.com/NVlabs/curobo','commit':COMMIT,'apiFamily':'legacy MotionGen','license':{'scope':'upstream research/evaluation non-commercial terms; explicitly selected recipe'}},
        'targetEnvironment':versions,'verification':{'status':'passed_with_explicit_device_scope','purpose':'SYSTEM_VERIFICATION_ONLY',
            'actualLoadedIdentity':native,'receipts':receipts,'assignedDevice':q['device'],
            'assessment':{'decision':'PASS_WITH_EXPLICIT_DEVICE_SCOPE','method':'all existing numerical/native checks completed; same prefix/ABI/device; actual source and binary hashes rechecked',
                'scope':'selected-prefix library/SDK coexistence, numerical and native Franka calibration checks only; no task-success, other robots or production deployment claim'}},
        'sourceFiles':sources}


def main(q):
    directory = Path(q['destination'])
    directory.mkdir(parents=True, exist_ok=False)
    if directory.resolve() != directory:
        raise ValueError('setup GPU output must be a plain new directory')
    source = directory / 'source' / 'runtime'
    source.mkdir(parents=True)
    for name, encoded in q['runtimeFiles'].items():
        if name not in ('jobs.py', 'records.py', 'scene.py', 'gpu_reservation.py'):
            raise ValueError('unknown lease module')
        (source / name).write_bytes(base64.b64decode(encoded))
    sys.path.insert(0, str(source.parent))
    from runtime.jobs import assign_device, _token, _group_members, stop_process_group
    from runtime.gpu_reservation import configured_peak, process_lease, reserve_gpu


    def interrupted(signum, frame):
        raise InterruptedError('explicit setup action interrupted')


    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, interrupted)
    environment = dict(os.environ)
    assign_device(q['device'], environment)
    environment.update(OMNI_KIT_ACCEPT_EULA='YES', PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    if q.get('probeSource'):
        (directory/'probe_motion_backend.py').write_text(q['probeSource'])
    root = Path(q['root'])
    peak = q.get('peakMiB') or configured_peak(root, {})
    if peak is None:
        raise ValueError('supply a GPU peak memory reservation before running setup')
    with reserve_gpu(root, q['device'], peak, legacy_barrier=True) as lease:
        try:
            for argv in q['commands']:
                print(json.dumps({'command': argv, 'device': q['device'], 'peakMiB': peak}), flush=True)
                process = subprocess.Popen(argv, start_new_session=True, **lease.child_options(environment))
                identity = {'pid': process.pid, 'processToken': _token(process.pid), 'members': {}}
                try:
                    while process.poll() is None:
                        identity['members'].update(_group_members(process.pid))
                        time.sleep(.1)
                    if process.returncode != 0:
                        raise RuntimeError('selected GPU setup command failed: ' + str(process.returncode))
                finally:
                    identity['members'].update(_group_members(process.pid))
                    stop_process_group(identity, process)
                    process.wait(timeout=3)
        finally:
            lease.finished = not any(
                process_lease(entry.name) == lease.id for entry in Path('/proc').iterdir() if entry.name.isdecimal()
            )
            if not lease.finished:
                raise RuntimeError('GPU_SETUP_EXIT_UNCONFIRMED')
    if q.get('verify'):
        manifest=build_manifest(q,directory)
        (directory/'motion-dependencies.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'completed':True,'scope':'system setup only; no task-success claim'}), flush=True)

if __name__=='__main__':
    main(json.loads(sys.argv[1]))
