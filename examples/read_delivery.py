"""Read producer-owned exports. No conversion, installation or task-quality decision."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import subprocess
from contextlib import contextmanager


def relative(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('delivery requires a plain relative path')
    return Path(value)


def plain(root, name):
    path = root/relative(name)
    if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root)):
        raise ValueError('delivery cannot contain symlinks')
    return path


@contextmanager
def open_plain(root, name):
    """Resolve every producer-controlled component through held directory descriptors."""
    plain(root, name)
    descriptors = []
    try:
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            parts = relative(name).parts
            for part in parts[:-1]:
                descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                descriptors.append(descriptor)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        except OSError as error:
            raise ValueError('delivery file cannot be opened without following links: '+str(name)) from error
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError('delivery requires a regular file')
        with os.fdopen(descriptor, 'rb') as stream:
            yield stream
    finally:
        for descriptor in reversed(descriptors): os.close(descriptor)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def _archive_name(project_id, ref, kind=None, expected=None):
    if not isinstance(ref, str):
        raise ValueError('archive reference must be a path string')
    if ref.startswith('projects/'):
        prefix = 'projects/'+project_id+'/'
        if not ref.startswith(prefix): raise ValueError('archive reference belongs to another project')
        ref = ref[len(prefix):]
    match = re.fullmatch(r'\.trace/(sources|inputs)/([a-f0-9]{64})\.json', ref)
    if not match or kind is not None and match[1] != kind:
        raise ValueError('archive reference requires its exact source/input path')
    if expected is not None and expected != match[2]:
        raise ValueError('archive declared hash differs from its reference')
    return ref


def _archive_refs(project_id, value):
    if isinstance(value, list):
        for item in value: yield from _archive_refs(project_id, item)
    elif isinstance(value, dict):
        if value.get('type') == 'source_snapshot':
            if not isinstance(value.get('sourceHash'), str) or not re.fullmatch('[a-f0-9]{64}', value['sourceHash']):
                raise ValueError('source snapshot hash is missing or invalid')
            yield _archive_name(project_id, value.get('snapshotRef'), 'sources', value['sourceHash'])
            previous = value.get('previousSourceHash')
            if previous is not None:
                if not isinstance(previous, str): raise ValueError('previous source hash must be a string')
                yield _archive_name(project_id, '.trace/sources/'+previous+'.json')
        elif value.get('type') == 'input_snapshot':
            if not isinstance(value.get('inputHash'), str) or not re.fullmatch('[a-f0-9]{64}', value['inputHash']):
                raise ValueError('input snapshot hash is missing or invalid')
            yield _archive_name(project_id, value.get('snapshotRef'), 'inputs', value['inputHash'])
        for key, item in value.items():
            if key in ('snapshotRef', 'inputRef', 'instructionsRef', 'beforeSourceRef', 'afterSourceRef') and item is not None:
                declared_input = key == 'inputRef' and value.get('type') == 'native_input'
                declared_instructions = key == 'instructionsRef' and value.get('kind') == 'native_turn_input'
                if declared_input or declared_instructions or isinstance(item, str) and '.trace' in item.split('/'):
                    name = _archive_name(project_id, item, 'inputs' if key in ('inputRef', 'instructionsRef') else None)
                    expected = None
                    if key in ('snapshotRef', 'inputRef'):
                        expected = value.get('inputHash', value.get('snapshotHash')) if '/inputs/' in name else value.get('sourceHash')
                    elif key in ('beforeSourceRef', 'afterSourceRef'):
                        expected = value.get(key.replace('Ref', 'Hash'))
                    yield _archive_name(project_id, name, expected=expected)
            elif key not in ('sourceFiles', 'beforeSources', 'afterSources') and isinstance(item, (dict, list)):
                yield from _archive_refs(project_id, item)


def _archive_content(name, data):
    value = json.loads(data)
    if not isinstance(value, dict): raise ValueError('archive must contain an object')
    sha = Path(name).stem
    if name.startswith('.trace/inputs/'):
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError('input archive byte hash mismatch: '+name)
    else:
        files = value.get('sourceFiles')
        if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
            raise ValueError('sourceFiles must contain file names and text')
        actual = hashlib.sha256(json.dumps(files, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        if value.get('sourceHash') != sha or actual != sha:
            raise ValueError('source hash mismatch: '+name)
    return value


def _archive_closure(project_id, values, read):
    # Both readers run as standalone sources remotely; keep these archive semantics identical.
    pending = set()
    for value in values: pending.update(_archive_refs(project_id, value))
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen: continue
        value = _archive_content(name, read(name))
        seen.add(name)
        if name.startswith('.trace/inputs/'):
            pending.update(set(_archive_refs(project_id, value))-seen)
    return dict(scope='raw_prefix_source_input_archives', complete=True, archiveCount=len(seen),
                sourceCount=sum(name.startswith('.trace/sources/') for name in seen),
                inputCount=sum(name.startswith('.trace/inputs/') for name in seen))


def required_outputs(binding):
    outputs = binding.get('requiredOutputs', ['generation', 'demonstrations', 'evaluation'])
    if (not isinstance(outputs, list) or any(not isinstance(value, str) for value in outputs)
            or len(outputs) != len(set(outputs)) or set(outputs) not in (
                {'generation', 'demonstrations', 'evaluation'}, {'generation', 'evaluation'}, {'demonstrations'})):
        raise ValueError('unsupported produced artifact output scope')
    if 'videoOnly' in binding and (binding['videoOnly'] is not True or 'demonstrations' not in outputs):
        raise ValueError('video-only delivery requires robot execution')
    if 'requiredOutputs' in binding or 'environmentRefs' in binding:
        refs = binding.get('environmentRefs')
        if not isinstance(refs, list) or not refs:
            raise ValueError('selected output binding requires environment references')
        revisions = []
        for row in refs:
            if (not isinstance(row, dict) or len(relative(row.get('revision')).parts) != 1
                    or row.get('split') not in (None, 'train', 'validation', 'test')):
                raise ValueError('invalid selected environment reference')
            revisions.append(row['revision'])
        if len(revisions) != len(set(revisions)):
            raise ValueError('selected environment revisions must have one split each')
    return outputs


def _manifest(directory, sha256, binding, episodes):
    with open_plain(directory, 'manifest.json') as stream: data = stream.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError('produced manifest digest differs')
    manifest = json.loads(data)
    if (manifest.get('schema') != 'sceneagent-produced-artifacts-v1'
            or manifest.get('binding') != binding or manifest.get('episodes') != episodes):
        raise ValueError('produced manifest source or selected episodes differ')
    required_outputs(binding)
    files = manifest.get('files')
    if not isinstance(files, list) or not files:
        raise ValueError('produced manifest lacks its file closure')
    names = set()
    for row in files:
        name = str(relative(row['path']))
        if (name in names or name == 'manifest.json' or re.fullmatch('read-[a-f0-9]{64}\\.json', name)
                or type(row.get('bytes')) is not int or row['bytes'] < 0
                or not re.fullmatch('[a-f0-9]{64}', row.get('sha256', ''))):
            raise ValueError('invalid or duplicate produced file declaration')
        names.add(name)
    return manifest


def _generation_archives(directory, manifest, project=None):
    if 'generation' not in required_outputs(manifest['binding']): return None
    products = manifest.get('products')
    generated = products.get('generation') if isinstance(products, dict) else None
    if not isinstance(generated, dict): raise ValueError('required generation product is missing')
    files = {row['path']: row for row in manifest['files']}
    raw_name = str(relative(generated.get('rawTrace')))
    if raw_name not in files: raise ValueError('raw trace is absent from file closure')
    archive_root = generated.get('archiveRoot')
    if archive_root is not None: archive_root = relative(archive_root)
    def read(name):
        if archive_root is None: raise ValueError('referenced archives require generation.archiveRoot')
        packaged = (archive_root/name).as_posix()
        if packaged not in files: raise ValueError('referenced archive is absent from file closure: '+name)
        def data(root, ref):
            with open_plain(root, ref) as stream:
                before = os.fstat(stream.fileno()); value = stream.read(); after = os.fstat(stream.fileno())
            identity = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_ctime_ns)
            if identity(before) != identity(after): raise ValueError('archive changed while reading: '+ref)
            return value
        value = data(directory, packaged); row = files[packaged]
        if len(value) != row['bytes'] or hashlib.sha256(value).hexdigest() != row['sha256']:
            raise ValueError('archive differs from its file declaration: '+name)
        if project is not None and value != data(project, name):
            raise ValueError('archive differs from the original project bytes: '+name)
        return value
    digest = hashlib.sha256(); size = 0
    def rows():
        nonlocal size
        with open_plain(directory, raw_name) as stream:
            for line in stream:
                if not line.endswith(b'\n'): raise ValueError('raw trace must end at a complete line')
                digest.update(line); size += len(line)
                row = json.loads(line)
                if not isinstance(row, dict): raise ValueError('raw trace events must be objects')
                yield row
    closure = _archive_closure(manifest['binding']['projectId'], rows(), read)
    trace = manifest.get('trace')
    if not isinstance(trace, dict) or type(trace.get('bytes')) is not int or size <= 0 or trace['bytes'] != size or trace.get('sha256') != digest.hexdigest():
        raise ValueError('raw trace differs from its exact prefix declaration')
    if project is not None:
        remaining, value, last = trace['bytes'], hashlib.sha256(), b''
        with open_plain(project, 'generation-trace.jsonl') as stream:
            while remaining:
                block = stream.read(min(1024*1024, remaining))
                if not block: raise ValueError('exported trace extends beyond captured facts')
                remaining -= len(block); value.update(block); last = block[-1:]
        if value.hexdigest() != trace['sha256'] or last != b'\n':
            raise ValueError('exported trace is not a complete-line prefix of captured facts')
    return closure


def verify(directory, sha256, binding, episodes):
    manifest = _manifest(directory, sha256, binding, episodes)
    for row in manifest['files']:
        value = hashlib.sha256()
        with open_plain(directory, row['path']) as stream:
            if os.fstat(stream.fileno()).st_size != row['bytes']:
                raise ValueError('produced file size differs: '+row['path'])
            for block in iter(lambda: stream.read(1024*1024), b''): value.update(block)
        if value.hexdigest() != row['sha256']: raise ValueError('produced file differs: '+row['path'])
    _generation_archives(directory, manifest)
    return manifest


def freeze(project, pointer, binding, episodes):
    """Copy only a declared closure; large arrays and videos never enter host memory."""
    if not re.fullmatch('[a-f0-9]{64}', pointer.get('sha256', '')):
        raise ValueError('invalid produced manifest digest')
    name = relative(pointer['manifestRef'])
    if name.parts[:3] != ('projects', project.name, 'delivery') or name.name != 'manifest.json':
        raise ValueError('produced manifest must belong to this project delivery directory')
    source_prefix = Path(*name.parts[2:-1])
    with open_plain(project, str(source_prefix/'manifest.json')) as stream: data = stream.read()
    if hashlib.sha256(data).hexdigest() != pointer['sha256']:
        raise ValueError('produced manifest digest differs')
    destination = project/'deliveries/artifacts'/pointer['sha256']
    if destination.exists():
        manifest = verify(destination, pointer['sha256'], binding, episodes)
        _generation_archives(destination, manifest, project)
        return destination, manifest
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.freeze-', dir=destination.parent))
    try:
        (temporary/'manifest.json').write_bytes(data)
        manifest = _manifest(temporary, pointer['sha256'], binding, episodes)
        for row in manifest['files']:
            target = temporary/row['path']; target.parent.mkdir(parents=True, exist_ok=True)
            value = hashlib.sha256()
            with open_plain(project, str(source_prefix/row['path'])) as incoming, target.open('xb') as outgoing:
                before = os.fstat(incoming.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != row['bytes']:
                    raise ValueError('produced source is not the declared regular file')
                for block in iter(lambda: incoming.read(1024*1024), b''):
                    value.update(block); outgoing.write(block)
                after = os.fstat(incoming.fileno())
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if identity(before) != identity(after) or value.hexdigest() != row['sha256']:
                raise ValueError('produced file changed during freeze: '+row['path'])
        _generation_archives(temporary, manifest, project)
        if destination.exists():
            verify(destination, pointer['sha256'], binding, episodes)
        else:
            temporary.rename(destination)
        return destination, manifest
    finally:
        if temporary.exists(): shutil.rmtree(temporary)


def validate_training_row(row, event_ids):
    """Check a training container, leaving target-model length and causal quality to its author/reviewer."""
    refs = row.get('sourceEventIds') if isinstance(row, dict) else None
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in event_ids for ref in refs):
        raise ValueError('training JSONL lacks real source events')
    tools = row.get('tools')
    if tools is not None:
        if not isinstance(tools, list): raise ValueError('training tools must be function declarations')
        for tool in tools:
            function = tool.get('function') if isinstance(tool, dict) else None
            if (not isinstance(function, dict) or tool.get('type', 'function') != 'function'
                    or not isinstance(function.get('name'), str) or not function['name']
                    or 'parameters' in function and not isinstance(function['parameters'], dict)):
                raise ValueError('invalid training function declaration')
    if 'messages' not in row:
        if not isinstance(row.get('prompt'), str) or not isinstance(row.get('completion'), str):
            raise ValueError('training JSONL requires messages or prompt/completion strings')
        return
    if not isinstance(row['messages'], list) or not row['messages']:
        raise ValueError('training messages must be a nonempty list')
    for message in row['messages']:
        if not isinstance(message, dict) or message.get('role') not in ('system', 'developer', 'user', 'assistant', 'tool'):
            raise ValueError('training message requires a supported role')
        calls = message.get('tool_calls')
        if calls is not None:
            if not isinstance(calls, list) or calls and message['role'] != 'assistant':
                raise ValueError('training tool_calls must be assistant function calls')
            for call in calls:
                function = call.get('function') if isinstance(call, dict) else None
                if (not isinstance(function, dict) or call.get('type', 'function') != 'function'
                        or not isinstance(function.get('name'), str) or not function['name']
                        or not isinstance(function.get('arguments'), (str, dict))):
                    raise ValueError('training tool call requires a function name and arguments')
        content = message.get('content')
        if content is None and calls: continue
        if isinstance(content, str): continue
        if not isinstance(content, list) or not content:
            raise ValueError('training message requires text or multimodal content')
        for part in content:
            if (not isinstance(part, dict) or not isinstance(part.get('type'), str) or not part['type']
                    or len(part) < 2 or part['type'] in ('text', 'input_text') and not isinstance(part.get('text'), str)):
                raise ValueError('invalid training content part')
            if part['type'] == 'image_url' and (not isinstance(part.get('image_url'), dict)
                    or not isinstance(part['image_url'].get('url'), str) or not part['image_url']['url']):
                raise ValueError('invalid training image URL content')


def read_formats(directory, manifest, evaluator_source, *, project=None):
    """Official readers consume local bytes; producer self-reported passes are ignored."""
    from importlib.metadata import version

    outputs = required_outputs(manifest['binding']); products = manifest['products']
    checks = dict(libraries={})

    names = {row['path'] for row in manifest['files']}
    def file(name):
        if str(relative(name)) not in names:
            raise ValueError('required format entry is absent from closure: '+str(name))
        return plain(directory, name)
    def tree(name):
        relative(name)
        if not any(n.startswith(name+'/') for n in names):
            raise ValueError('required format directory is absent from closure: '+name)
        return plain(directory, name)

    if 'generation' in outputs:
        from harbor.models.trajectories import Trajectory

        generated = products['generation']
        event_ids = set(); current_generation = False
        with file(generated['rawTrace']).open() as stream:
            for line in stream:
                row = json.loads(line)
                if row.get('eventId'): event_ids.add(row['eventId'])
                current_generation |= row.get('generationId') == manifest['binding']['generationId']
        if not current_generation:
            raise ValueError('raw trace does not include this generation')
        atif_path = file(generated['atif'])
        trajectory = Trajectory.model_validate_json(atif_path.read_bytes())
        if trajectory.schema_version != 'ATIF-v1.7' or not trajectory.steps:
            raise ValueError('expected a nonempty pinned ATIF-v1.7 trajectory')
        def media_closure(value):
            if isinstance(value, list):
                for item in value: media_closure(item)
            elif isinstance(value, dict):
                if value.get('type') == 'image' and isinstance(value.get('source'), dict):
                    image = (atif_path.parent/value['source']['path']).resolve()
                    if not image.is_relative_to(directory): raise ValueError('ATIF image is outside frozen closure')
                    file(image.relative_to(directory).as_posix())
                for item in value.values(): media_closure(item)
        media_closure(trajectory.model_dump())
        training_rows = 0
        with file(generated['training']).open() as stream:
            for line in stream:
                row = json.loads(line)
                validate_training_row(row, event_ids)
                training_rows += 1
        if not training_rows: raise ValueError('empty training JSONL')

        checks['libraries']['harbor'] = version('harbor')
        checks['generation'] = dict(atifSteps=len(trajectory.steps), trainingRows=training_rows,
                                    archiveClosure=_generation_archives(directory, manifest))

    if 'demonstrations' in outputs and manifest['binding'].get('videoOnly'):
        groups = products.get('robot'); seen = set(); reports = []
        selected = {row['episodeId']: row for row in manifest['episodes']}
        if not project or not selected or len(selected) != len(manifest['episodes']) or not isinstance(groups, list) or not groups:
            raise ValueError('video delivery lacks its selected source episodes')
        for group in groups:
            if set(group) != {'video', 'mapping'}: raise ValueError('video delivery requires video and source mapping only')
            mapping = json.loads(file(group['mapping']).read_text()); rows = mapping.get('episodes')
            if not isinstance(rows, list) or len(rows) != 1: raise ValueError('each full video must bind one source episode')
            row = rows[0]; episode_id = row.get('episodeId')
            if episode_id not in selected or episode_id in seen or any(row.get(k) != v for k, v in selected[episode_id].items()):
                raise ValueError('video source selection differs')
            source = row.get('sourceVideo'); prefix = 'episodes/'+episode_id+'/'
            if not isinstance(source, str) or not source.startswith(prefix): raise ValueError('video must come from its selected episode')
            video = file(group['video'])
            if digest(video) != digest(plain(project, source)): raise ValueError('full source video bytes differ')
            result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_frames',
                '-show_entries', 'stream=width,height,nb_read_frames', '-of', 'json', str(video)],
                capture_output=True, text=True, check=True, timeout=600)
            streams = json.loads(result.stdout).get('streams', [])
            if len(streams) != 1 or int(streams[0].get('nb_read_frames', 0)) < 2 or min(streams[0]['width'], streams[0]['height']) <= 0:
                raise ValueError('full video has no decodable motion frames')
            subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(video), '-map', '0:v:0', '-f', 'null', '-'],
                capture_output=True, check=True, timeout=600)
            seen.add(episode_id); reports.append(dict(episodeId=episode_id, frames=int(streams[0]['nb_read_frames']), sourceBytesMatch=True))
        if seen != set(selected): raise ValueError('selected episodes lack full videos')
        checks['robot'] = reports

    if 'demonstrations' in outputs and not manifest['binding'].get('videoOnly'):
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        import h5py
        import numpy as np
        import robomimic.utils.obs_utils as ObsUtils
        from robomimic.utils.dataset import SequenceDataset

        scoped = 'environmentRefs' in manifest['binding']
        def identity(row): return (row.get('revision'), row['episodeId']) if scoped else row['episodeId']
        selected = {identity(row) for row in manifest['episodes']}; seen = set(); groups = []
        revisions = {row['revision'] for row in manifest['binding'].get('environmentRefs', [])}
        if (not selected or len(selected) != len(manifest['episodes'])
                or scoped and any(row.get('revision') not in revisions for row in manifest['episodes'])):
            raise ValueError('robot training groups do not match selected source episodes')
        if not isinstance(products.get('robot'), list) or not products['robot']:
            raise ValueError('both robot training formats are required')
        for group in products['robot']:
            mapping = json.loads(file(group['mapping']).read_text())
            rows = mapping['episodes']; ids = [row['episodeId'] for row in rows]; identities = [identity(row) for row in rows]
            if (not rows or len(identities) != len(set(identities)) or seen.intersection(identities)
                    or not set(identities).issubset(selected)
                    or {row['lerobotEpisodeIndex'] for row in rows} != set(range(len(rows)))
                    or len({row['hdf5Demo'] for row in rows}) != len(rows)):
                raise ValueError('robot training groups do not match selected source episodes')
            seen.update(identities)
            dataset_root = tree(group['lerobot'])
            info = json.loads(file(group['lerobot']+'/meta/info.json').read_text())
            for key in ('data_path', 'video_path'):
                if info.get(key): relative(info[key])
            data = LeRobotDataset('local/delivery', root=dataset_root, download_videos=False, video_backend='pyav')
            if data.num_episodes != len(rows) or len(data) != sum(row['steps'] for row in rows):
                raise ValueError('LeRobot episode or step count differs from the source mapping')
            hdf5_path = file(group['hdf5'])
            with h5py.File(hdf5_path, 'r') as hdf:
                visited = set()
                def reject_external(group):
                    identity = hash(group.id)
                    if identity in visited: return
                    visited.add(identity)
                    for key in group:
                        link = group.get(key, getlink=True)
                        if isinstance(link, h5py.ExternalLink):
                            raise ValueError('HDF5 delivery requires internal datasets')
                        child = group[key]
                        if isinstance(child, h5py.Group): reject_external(child)
                        elif child.is_virtual or child.external:
                            raise ValueError('HDF5 external storage is outside the declared closure')
                reject_external(hdf)
                if set(hdf['data']) != {row['hdf5Demo'] for row in rows}:
                    raise ValueError('HDF5 episodes differ from source mapping')
                for row in rows:
                    demo = hdf['data'][row['hdf5Demo']]
                    if (type(row['steps']) is not int or row['steps'] <= 0
                            or len(demo['actions']) != row['steps'] or demo.attrs['num_samples'] != row['steps']
                            or set(demo['obs']) != set(mapping['hdf5Obs'])):
                        raise ValueError('HDF5 sample or observation mapping differs')
                    if not np.isfinite(demo['actions'][0]).all():
                        raise ValueError('HDF5 action sample is nonfinite')
            specs = {}
            for key, modality in mapping['hdf5Obs'].items(): specs.setdefault(modality, []).append(key)
            ObsUtils.initialize_obs_utils_with_obs_specs({'obs': specs})
            sequence = SequenceDataset(hdf5_path=str(hdf5_path), obs_keys=list(mapping['hdf5Obs']),
                action_keys=('actions',), action_config={'actions': {'normalization': None}},
                dataset_keys=(), load_next_obs=False, hdf5_cache_mode=None,
                hdf5_normalize_obs=False, hdf5_use_swmr=False, frame_stack=1, seq_length=1,
                pad_frame_stack=True, pad_seq_length=True)
            try:
                for row in rows:
                    index = row['lerobotEpisodeIndex']
                    if type(index) is not int or not 0 <= index < data.num_episodes:
                        raise ValueError('invalid LeRobot episode mapping')
                    start = int(data.meta.episodes[index]['dataset_from_index'])
                    for at in (start, start+row['steps']-1):
                        sample = data[at]
                        if int(sample['episode_index']) != index or not np.isfinite(sample['action'].numpy()).all():
                            raise ValueError('LeRobot mapped sample is invalid')
                if not len(sequence) or not np.isfinite(sequence[0]['actions']).all():
                    raise ValueError('HDF5 SequenceDataset sample is invalid')
            finally:
                sequence.close_and_delete_hdf5_handle()
            groups.append(dict(episodes=ids, samples=len(data), readers=['LeRobotDataset', 'robomimic.SequenceDataset']))
        if seen != selected: raise ValueError('selected episodes lack both robot formats')

        checks['libraries'].update({name: version(name) for name in ('lerobot', 'h5py')})
        checks['robomimicReaderSha256'] = digest(Path(__import__('inspect').getfile(SequenceDataset)))
        checks['robot'] = groups

    if 'evaluation' in outputs:
        environment = products['environment']
        environment_root = tree(environment['root'])
        for key in ('readme', 'runtime'): file(environment[key])
        if file(environment['entrypoint']).read_bytes() != evaluator_source.encode():
            raise ValueError('environment entry must be the preserved standard evaluate.py')
        for reference in manifest['binding'].get('environmentRefs', [dict(revision=manifest['binding']['revision'])]):
            build = environment_root/'projects'/manifest['binding']['projectId']/'revisions'/reference['revision']/'manifest.json'
            file(str(build.relative_to(directory)))
        checks['environment'] = dict(root=environment['root'], scope='closure_read; native_new_root_probe_required')
    return checks


def consume(project, pointer, binding, episodes, consumer_digest, evaluator_source, *, frozen_ref=None):
    if frozen_ref is None:
        directory, manifest = freeze(project, pointer, binding, episodes)
        # Some upstream readers print progress. Keep the transport reply a single JSON object.
        import contextlib
        import sys
        with contextlib.redirect_stdout(sys.stderr):
            checks = read_formats(directory, manifest, evaluator_source, project=project)
        report = dict(schema='sceneagent-produced-consumption-v1', passed=True, consumerDigest=consumer_digest,
                      binding=binding, episodes=episodes, checks=checks)
        report_name = 'read-'+consumer_digest+'.json'
        (directory/report_name).write_text(json.dumps(report, ensure_ascii=False, allow_nan=False))
    else:
        expected = Path('projects')/project.name/'deliveries/artifacts'/pointer['sha256']/'manifest.json'
        if relative(frozen_ref) != expected: raise ValueError('frozen produced manifest scope differs')
        directory = project/'deliveries/artifacts'/pointer['sha256']
        manifest = verify(directory, pointer['sha256'], binding, episodes)
        report_name = 'read-'+consumer_digest+'.json'
        report = json.loads(plain(directory, report_name).read_text())
        if (report.get('consumerDigest') != consumer_digest or report.get('binding') != binding
                or report.get('episodes') != episodes or report.get('passed') is not True):
            raise ValueError('frozen format consumption report differs')
    prefix = directory.relative_to(project.parent.parent).as_posix()
    def references(value):
        if isinstance(value, dict): return {key: references(item) for key, item in value.items()}
        if isinstance(value, list): return [references(item) for item in value]
        return prefix+'/'+str(relative(value))
    outputs = required_outputs(binding)
    products = {key: manifest['products'][key] for output, key in (('generation', 'generation'),
        ('demonstrations', 'robot'), ('evaluation', 'environment')) if output in outputs}
    return {**report, **pointer, 'frozenManifestRef': prefix+'/manifest.json',
            **({'environmentRoot': prefix+'/'+products['environment']['root']} if 'evaluation' in outputs else {}),
            'products': references(products), 'artifactRef': prefix+'/'+report_name,
            'files': [dict(artifactRef=prefix+'/'+row['path'], bytes=row['bytes'], sha256=row['sha256'])
                      for row in manifest['files']]}
