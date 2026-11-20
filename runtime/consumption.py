"""Consume frozen versions with their original kernels inside one supervised job."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from runtime import worker
from runtime.jobs import _token, payload_digest, worker_timeout
from runtime.scene import valid_id


def needs_dispatch(payload, root):
    if payload.get('operation') != 'consume' or not payload.get('projectId'):
        return False
    project = root/'projects'/valid_id(payload['projectId'])
    refs = payload.get('environmentRefs', [{'revision':payload.get('revision')}])
    for ref in refs:
        manifest = json.loads((project/'revisions'/valid_id(ref['revision'])/'manifest.json').read_text())
        snapshot = manifest.get('runtimeSnapshotRef')
        if snapshot is not None and worker.within(root,snapshot) != Path(worker.__file__).resolve().parent.parent:
            return True
    return False


def _runtime_binding(manifest, root):
    reference = manifest['runtimeSnapshotRef']
    if not isinstance(reference, str) or not re.fullmatch(r'runtime/[a-f0-9]{64}', reference):
        raise ValueError('frozen runtime must be a content-addressed snapshot')
    snapshot = root/reference/'runtime'
    execution = manifest['runtimeExecution']
    if not Path(execution['python']).is_absolute() or not Path(execution['prefix']).is_absolute():
        raise ValueError('frozen interpreter must be absolute')
    if snapshot.is_symlink() or snapshot.parent.is_symlink() or snapshot.resolve() != snapshot:
        raise ValueError('frozen runtime snapshot cannot be redirected')
    files = sorted(p for p in snapshot.iterdir() if p.suffix == '.py' or p.name == 'motion-dependencies.json')
    checksum = hashlib.sha256()
    for file in files:
        if file.is_symlink() or not file.is_file():
            raise ValueError('frozen runtime code must be ordinary files')
        checksum.update(file.name.encode())
        checksum.update(file.read_bytes())
    if checksum.hexdigest() != Path(reference).name or not (snapshot/'worker.py').is_file():
        raise ValueError('frozen runtime snapshot digest mismatch')
    return {'runtimeSnapshotRef':reference, 'runtimeExecution':execution, 'runtime':manifest['runtime'], 'files':set(files)}


def frozen_worker(payload, root):
    if (payload.get('operation') not in ('rollout', 'observe') or payload.get('output') == 'web_preview'
            or not payload.get('projectId') or not payload.get('revision')):
        return None
    project, revision = valid_id(payload['projectId']), valid_id(payload['revision'])
    path = worker.within(root, 'projects/'+project+'/revisions/'+revision+'/manifest.json')
    manifest = json.loads(path.read_text())
    reference = manifest.get('runtimeSnapshotRef')
    if reference is None or worker.within(root, reference) == Path(worker.__file__).resolve().parent.parent:
        return None
    if (manifest.get('schema') != 'sceneagent-build-v6' or manifest.get('engine') != 'isaacsim'
            or manifest.get('physics') != 'physx' or manifest.get('status') != 'completed'
            or (manifest.get('projectId'), manifest.get('revision')) != (project, revision)):
        raise ValueError('operation requires the requested published frozen environment')
    worker.validate_publication(manifest, root)
    return _runtime_binding(manifest, root)


def plan(payload, root):
    outputs, refs = worker._consumption_scope(payload)
    project = root/'projects'/valid_id(payload['projectId'])
    groups, by_revision, pairs = {}, {}, set()
    for ref in refs:
        revision = valid_id(ref['revision'])
        path = project/'revisions'/revision/'manifest.json'
        manifest = json.loads(path.read_text())
        if (manifest.get('schema')!='sceneagent-build-v6' or manifest.get('engine')!='isaacsim'
                or manifest.get('physics')!='physx' or manifest.get('status')!='completed'
                or (manifest['projectId'],manifest['revision'])!=(payload['projectId'],revision)):
            raise ValueError('consumer requires the requested published frozen environment')
        worker.validate_publication(manifest,root)
        reference = manifest['runtimeSnapshotRef']
        if not isinstance(reference,str) or not re.fullmatch(r'runtime/[a-f0-9]{64}',reference):
            raise ValueError('frozen consumer runtime must be a content-addressed snapshot')
        execution = manifest['runtimeExecution']
        key = (reference,execution['python'])
        if key not in groups:
            request_id='runtime_'+hashlib.sha256((payload['requestId']+'\n'+reference+'\n'+execution['python']).encode()).hexdigest()
            groups[key]={**_runtime_binding(manifest, root), 'payload':{**payload,'requestId':request_id,'revision':revision,
                    'requiredOutputs':outputs,'environmentRefs':[],'episodes':[],'validationEpisodes':[]}}
        group=groups[key]
        if group['runtimeExecution']!=execution or group['runtime']!=manifest['runtime']:
            raise ValueError('one frozen consumer snapshot has inconsistent execution identities')
        ids={row['id'] for row in manifest['tasks']};selected=set(ref.get('taskIds',ids))
        if selected-ids or 'evaluation' in outputs and selected!=ids:
            raise ValueError('consumer environment task coverage differs from its frozen tasks')
        pairs.update((revision,id) for id in selected)
        group['files'].add(path);group['payload']['environmentRefs'].append(ref);by_revision[revision]=group
    if payload['revision'] not in by_revision:
        raise ValueError('consumer primary revision is absent from its selection')
    selections=worker._consume_selection(payload) if 'demonstrations' in outputs else []
    if 'demonstrations' not in outputs and payload.get('episodes'):
        raise ValueError('unrequested demonstration episodes')
    for row in selections:
        if (row.get('revision'),row['taskId']) not in pairs:
            raise ValueError('behavior episode leaves requested revision/task scope')
        by_revision[row['revision']]['payload']['episodes'].append(row)
    ids=payload.get('validationEpisodes',[])
    if not isinstance(ids,list) or len(ids)!=len(set(ids)) or ids and 'evaluation' not in outputs:
        raise ValueError('invalid validation episode selection')
    for id in ids:
        path=project/'episodes'/valid_id(id)/'manifest.json'
        episode=json.loads(path.read_text())
        if (episode['revision'],episode['taskId']) not in pairs:
            raise ValueError('validation episode leaves requested revision/task scope')
        by_revision[episode['revision']]['payload']['validationEpisodes'].append(id)
    return list(groups.values())


_VERIFY = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from runtime import worker,jobs
p=json.load(sys.stdin);files=set()
def required(payload,result):
    files.update(worker.required_files(payload,result));return files
result=jobs.verify_candidate(Path(p['directory']),p['payload'],p['exitCode'],required)
jobs.atomic_json(Path(sys.argv[2]),{'result':result,'files':[str(p) for p in sorted(files)]})
'''


def verify_frozen_candidate(binding, directory, payload, exit_code):
    root = Path(payload['root']).resolve()
    snapshot = root/binding['runtimeSnapshotRef']
    python = binding['runtimeExecution']['python']
    with tempfile.TemporaryDirectory(prefix='verify-',dir=directory) as temporary:
        result_path = Path(temporary)/'result.json'
        with (directory/'verification.log').open('ab') as log:
            completed = subprocess.run([python,'-I','-B','-c',_VERIFY,str(snapshot),str(result_path)],
                input=json.dumps({'directory':str(directory),'payload':payload,'exitCode':exit_code}).encode(),
                stdout=log,stderr=subprocess.STDOUT,close_fds=True,timeout=worker_timeout(payload))
        if completed.returncode != 0:
            raise ValueError('original frozen worker verification failed; see '+str(directory/'verification.log'))
        verified = json.loads(result_path.read_text())
    return verified['result'], {worker.within(root,name) for name in verified['files']}


def native(group, directory, execute):
    """Children inherit the outer worker's process group, GPU lock and cancellation."""
    payload=group['payload'];root=Path(payload['root']).resolve()
    snapshot=root/group['runtimeSnapshotRef'];python=group['runtimeExecution']['python']
    request=directory/'worker-request.json';exit_path=directory/'exit.json'
    if execute:
        directory.mkdir(parents=True,exist_ok=False)
        worker.save(request,payload)
        worker.save(directory/'worker-parent.json',{'pid':os.getpid(),'processToken':_token(os.getpid())})
        command=[python,'-B',str(snapshot/'runtime/worker.py'),'--kit-worker',str(directory),
            '--portable','--portable-root',str(directory/'kit'),f'--/log/file={directory/"isaac-sdk.log"}']
        command += [value for value in sys.argv if value.startswith('--/rtx/shaderDb/')]
        with (directory/'kit.log').open('ab') as log:
            completed=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                close_fds=True,timeout=worker_timeout(payload))
        worker.save(exit_path,{'exitCode':completed.returncode,'requestHash':payload_digest(payload)})
    if json.loads(request.read_text())!=payload:
        raise ValueError('frozen consumer child request changed')
    exit_record=json.loads(exit_path.read_text())
    if exit_record!={'exitCode':0,'requestHash':payload_digest(payload)}:
        raise ValueError('frozen consumer child did not exit successfully; see '+str(directory/'kit.log'))
    result,files=verify_frozen_candidate(group,directory,payload,0)
    files.update((request,exit_path,directory/'candidate.json'))
    return result,files


def consume(payload, root, result=None):
    groups=plan(payload,root)
    directory=root/'projects'/payload['projectId']/'consumer-reports'/valid_id(payload['requestId'])
    reference=str((directory/'report.json').relative_to(root))
    if result is None:
        directory.mkdir(parents=True,exist_ok=False)
    elif result.get('artifactRef')!=reference or json.loads((directory/'report.json').read_text())!={k:v for k,v in result.items() if k!='artifactRef'}:
        raise ValueError('mixed runtime consumer report differs from its fixed scope or saved result')
    reports,required,runtimes=[],{directory/'report.json'},[]
    for group in groups:
        request=group['payload']
        report,files=native(group,directory/'runtimes'/request['requestId'],result is None)
        expected_ref=str((directory.parent/request['requestId']/'report.json').relative_to(root))
        if (report.get('passed') is not True or report.get('schema')!='sceneagent-product-consumption-v1'
                or report.get('artifactRef')!=expected_ref
                or any(report.get(key)!=request[key] for key in ('projectId','revision','requiredOutputs','environmentRefs','validationEpisodes'))):
            raise ValueError('original frozen consumer report differs from its requested scope')
        reports.append(report);required.update(files);required.update(group['files'])
        runtimes.append({key:group[key] for key in ('runtimeSnapshotRef','runtimeExecution','runtime')} | {
            'environmentRefs':request['environmentRefs'],'artifactRef':report['artifactRef'],
            'sha256':worker.digest(root/report['artifactRef'])})
    outputs,refs=worker._consumption_scope(payload)
    report={'schema':'sceneagent-product-consumption-v1','projectId':payload['projectId'],'revision':payload['revision'],
        'requiredOutputs':outputs,'environmentRefs':refs,'validationEpisodes':payload.get('validationEpisodes',[]),
        'runtimeGroups':runtimes,'limitations':list(dict.fromkeys(text for row in reports for text in row['limitations']))}
    if 'demonstrations' in outputs:
        batches=[batch for row in reports for batch in row['demonstrations']['batches']]
        order={row['episodeId']:index for index,row in enumerate(payload['episodes'])}
        batches.sort(key=lambda row:order[row['episodeId']])
        report['demonstrations']={'passed':all(row['demonstrations']['passed'] for row in reports),
            'episodeCount':len(batches),'batches':batches}
    if 'evaluation' in outputs:
        order={ref['revision']:index for index,ref in enumerate(refs)}
        merge=lambda key:sorted([value for row in reports for value in row['evaluation'][key]],key=lambda row:order[row['revision']])
        checks={key:value for row in reports for key,value in row['evaluation']['isolation']['checks'].items()}
        report['reload']={'passed':all(row['reload']['passed'] for row in reports),'newProcess':True,
            'environments':sorted([env for row in reports for env in row['reload']['environments']],key=lambda row:order[row['revision']]),
            'originalProjectDirectoryRequired':False}
        if payload.get('environmentRoot') is not None:
            if any(row['reload'].get('environmentRoot')!=payload['environmentRoot'] for row in reports):
                raise ValueError('frozen consumer used another exported environment root')
            report['reload']['environmentRoot']=payload['environmentRoot']
        report['evaluation']={'passed':all(row['evaluation']['passed'] for row in reports),'tasks':merge('tasks'),
            'episodeCount':sum(row['evaluation']['episodeCount'] for row in reports),'interfaces':merge('interfaces'),
            'isolation':{'passed':bool(checks) and all(checks.values()),'checks':checks,
                         'mechanism':'bubblewrap_unshare_all_explicit_dependency_mounts'}}
    report['passed']=all(report[key]['passed'] for key in ('demonstrations','reload','evaluation') if key in report)
    if result is None:
        worker.save(directory/'report.json',report)
        return {**report,'artifactRef':reference}
    if not worker._same_json(report,{k:v for k,v in result.items() if k!='artifactRef'}):
        raise ValueError('mixed runtime consumer summary differs from original verified evidence')
    return required
