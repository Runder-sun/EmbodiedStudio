"""Consume public generation samples using only a delivered trace and source archive."""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
import re
import stat
from pathlib import Path


def _reference_path(root, ref):
    if not isinstance(ref, str):
        raise ValueError('reference must be a path string')
    relative = Path(ref)
    path = (root.parent.parent/relative if ref.startswith('projects/') else root/relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root):
        raise ValueError(f'reference outside project scope: {ref}')
    return path


def _reference(root, ref, sha256=None):
    data = _reference_path(root, ref).read_bytes()
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f'reference hash mismatch: {ref}')
    return data


def _archive_bytes(root, name):
    descriptors = []
    try:
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            parts = name.split('/')
            for part in parts[:-1]:
                descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                descriptors.append(descriptor)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        except OSError as error:
            raise ValueError('archive cannot be opened without following links: '+name) from error
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            os.close(descriptor)
            raise ValueError('archive must be a regular file')
        with os.fdopen(descriptor, 'rb') as stream:
            data = stream.read(); after = os.fstat(stream.fileno())
            identity = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_ctime_ns)
            if identity(before) != identity(after): raise ValueError('archive changed while reading')
            return data
    finally:
        for descriptor in reversed(descriptors): os.close(descriptor)


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


def freeze_trace(project_directory, trace_ref='generation-trace.jsonl'):
    """Freeze active aliases once; return the reference, digest and exact captured bytes."""
    root = Path(project_directory).resolve()
    trace_bytes = _reference(root, trace_ref)
    trace_hash = hashlib.sha256(trace_bytes).hexdigest()
    if _reference_path(root, trace_ref) == (root/'generation-trace.jsonl').resolve():
        trace_ref = 'consumer-reports/traces/'+trace_hash+'.jsonl'
        frozen = _reference_path(root, trace_ref)
        frozen.parent.mkdir(parents=True, exist_ok=True)
        if frozen.exists() and frozen.read_bytes() != trace_bytes:
            raise ValueError('immutable frozen trace differs')
        if not frozen.exists():
            temp = frozen.with_name(frozen.name+'.tmp-'+str(os.getpid()))
            temp.write_bytes(trace_bytes)
            os.replace(temp, frozen)
    return trace_ref, trace_hash, trace_bytes


_UNRESOLVED_ARRAY_OWNER = object()


def _package_scope(root, value, inherited):
    artifacts = value.get('artifacts')
    manifest = artifacts.get('manifest') if isinstance(artifacts, dict) else None
    declared = str(value.get('schema', '')).startswith(('sceneagent-build-', 'sceneagent-episode-'))
    if not (isinstance(artifacts, dict) and 'manifest' in artifacts) and not declared:
        if 'projectId' in value and any(key in value for key in ('revision', 'episode', 'episodeId')):
            # Incomplete summaries may contain complete child receipts, but cannot own arrays.
            return _UNRESOLVED_ARRAY_OWNER
        return inherited
    revision = value.get('revision')
    episode = value.get('episodeId', value.get('episode'))
    if (value.get('projectId') != root.name or not isinstance(revision, str)
            or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', revision)
            or ('episodeId' in value and 'episode' in value and value['episodeId'] != value['episode'])):
        raise ValueError('array package identity is missing or conflicting')
    prefix = f'projects/{root.name}/'
    if episode is None:
        relative = f'revisions/{revision}/manifest.json'
    else:
        if not isinstance(episode, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', episode):
            raise ValueError('array package episode identity is invalid')
        relative = f'episodes/{episode}/manifest.json'
        parts = manifest.split('/') if isinstance(manifest, str) else []
        if len(parts) == 7 and parts[:3] == ['projects', root.name, 'evaluations']:
            evaluation = parts[3]
            if (not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', evaluation)
                    or value.get('evaluationId', evaluation) != evaluation):
                raise ValueError('array package evaluation identity is conflicting')
            relative = f'evaluations/{evaluation}/episodes/{episode}/manifest.json'
    if manifest != prefix+relative:
        raise ValueError('array package manifest differs from its declared identity')
    return (root/relative).parent


def _array_media(root, value, scope):
    if scope is _UNRESOLVED_ARRAY_OWNER:
        raise ValueError('array package identity is missing its manifest')
    ref = value['ref']
    if not isinstance(ref, str) or '\\' in ref or any(part in ('', '.', '..') for part in ref.split('/')):
        raise ValueError('array reference path is invalid')
    if ref.startswith('arrays/'):
        if scope is None:
            raise ValueError('local array reference is missing its package scope')
        path = scope/ref
    else:
        path = root.parent.parent/ref if ref.startswith('projects/') else root/ref
    if (not path.is_relative_to(root) or Path(ref).is_absolute()
            or (scope is not None and not path.is_relative_to(scope/'arrays'))
            or any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(root))):
        raise ValueError('array reference outside its project or package scope')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != value['sha256']:
        raise ValueError(f'array reference hash mismatch: {ref}')


def _media(root, value, array_scope=None):
    if isinstance(value, list):
        for item in value:
            _media(root, item, array_scope)
    elif isinstance(value, dict):
        array_scope = _package_scope(root, value, array_scope)
        if set(value) == {'ref', 'sha256', 'dtype', 'shape'}:
            _array_media(root, value, array_scope)
            return
        if value.get('type') == 'image' or ('data' in value and value.get('mimeType', '').startswith('image/')):
            encoded, mime = value.get('data'), value.get('mimeType', '')
            if encoded is None and value.get('url', '').startswith('data:image/'):
                header, encoded = value['url'].split(',', 1)
                if not header.endswith(';base64'):
                    raise ValueError('archived native image must use base64')
                mime = header[5:].split(';')[0]
            if not mime.startswith('image/'):
                raise ValueError('image content is missing archived data or MIME type')
            if encoded is None:
                if not value.get('ref') or not value.get('sha256'):
                    raise ValueError('archived image reference requires data reference and hash')
                data = _reference(root, value['ref'], value['sha256'])
            else:
                data = base64.b64decode(encoded, validate=True)
            if not data:
                raise ValueError('image content is empty')
            if value.get('sha256') and hashlib.sha256(data).hexdigest() != value['sha256']:
                raise ValueError('image content hash mismatch')
        if 'ref' in value and ('sha256' in value or value.get('type') in ('image', 'file')):
            _reference(root, value['ref'], value.get('sha256'))
        for key, item in value.items():
            if key in ('snapshotRef', 'imageRef', 'thumbnailRef', 'inputRef', 'resultRef', 'artifactRef', 'mediaRef') and item:
                content = _reference(root, item)
                image_ref = key in ('imageRef', 'thumbnailRef') or (key == 'artifactRef' and not (value.get('imageRef') or value.get('thumbnailRef')))
                if image_ref and isinstance(value.get('image'), dict) and 'data' in value['image']:
                    if content != base64.b64decode(value['image']['data'], validate=True):
                        raise ValueError('image content differs from the referenced artifact')
            elif key not in ('data', 'sourceFiles', 'beforeSources', 'afterSources'):
                _media(root, item, array_scope)


def _public_input(event):
    if event.get('type') in ('user_message', 'requirement_added'):
        return dict(eventId=event['eventId'], type='userProvided',
                    text=event.get('message', event.get('text', '')), sentToModel='unconfirmed')
    if event.get('type') == 'codex' and event.get('method') == 'transport/sent':
        message = event.get('params', {}).get('message', {})
        if message.get('method') in ('turn/start', 'turn/steer') and message.get('params', {}).get('input'):
            return dict(eventId=event['eventId'], type='nativeInput',
                        input=message['params']['input'], sentToModel='captured')
    return None


def _source_files(root, source_hash, ref):
    name = _archive_name(root.name, ref, 'sources', source_hash)
    return _archive_content(name, _archive_bytes(root, name))['sourceFiles']


def links(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('requestId', 'jobId', 'revision', 'episodeId') and isinstance(item, str):
                found.add(item)
            elif isinstance(item, (dict, list)):
                found.update(links(item))
    elif isinstance(value, list):
        for item in value:
            found.update(links(item))
    return found


def _related_events(rows, selected):
    related_types = ('operation_started', 'operation_completed', 'operation_failed', 'tool_call', 'tool_result')
    roots = {row.get('eventId') for row in selected if row.get('type') in related_types}
    roots.update(row.get('callEventId') for row in selected if row.get('type') in related_types)
    link_map = {row.get('eventId'): frozenset(links(row)) for row in rows if row.get('eventId') in roots}
    related = [(index, row, link_map[row['eventId']] | link_map.get(row.get('callEventId'), frozenset()))
               for index, row in enumerate(selected) if row.get('type') in related_types]
    return link_map, related


def _linked_outcomes(related, start, initial_links):
    known, outcomes = set(initial_links), []
    for index, candidate, identifiers in related:
        if index >= start and identifiers & known:
            outcomes.append(candidate['eventId'])
            known.update(identifiers)
    return outcomes


def _edit_links(related, start, source_hash, link_map):
    known = set()
    for index, candidate, identifiers in related:
        if index < start:
            continue
        if candidate.get('type') == 'tool_call' and candidate.get('args', {}).get('expectedSourceHash') == source_hash:
            known.update(link_map[candidate['eventId']])
            for returned_index, returned, unused in related:
                if returned_index >= start and returned.get('callEventId') == candidate['eventId']:
                    known.update(link_map[returned['eventId']])
        elif candidate.get('type') == 'operation_started' and candidate.get('sourceHash') == source_hash:
            known.update(link_map[candidate['eventId']])
    return known


def consume(project_directory, generation_id=None, trace_ref='generation-trace.jsonl'):
    root = Path(project_directory).resolve()
    issues, samples, snapshots = [], [], {}

    def issue(code, message, event=None, severity='error'):
        issues.append(dict(code=code, message=message, eventId=(event or {}).get('eventId'), severity=severity))

    try:
        trace_ref, trace_hash, trace_bytes = freeze_trace(root, trace_ref)
        rows = [json.loads(line) for line in trace_bytes.decode('utf8').splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError('trace events must be objects')
    except (OSError, ValueError) as error:
        return dict(schema='sceneagent-generation-consumer-v2', version=2, projectId=root.name, generationId=generation_id,
                    passed=False, sampleCount=0, editSampleCount=0, toolSampleCount=0,
                    traceRef=trace_ref,
                    coverage={'internalContext': 'unknown'}, issues=[dict(code='trace_unreadable', message=str(error))],
                    requestVersions=[], samples=[])
    event_map = {}
    for row in rows:
        event_id = row.get('eventId')
        if not isinstance(event_id, str) or not event_id.startswith(root.name+':') or row.get('projectId', root.name) != root.name or event_id in event_map:
            issue('event_identity', 'Missing, out-of-project or duplicate eventId', row)
        event_map[event_id] = row
    if any(item['code'] == 'event_identity' for item in issues):
        return dict(schema='sceneagent-generation-consumer-v2', version=2, projectId=root.name, generationId=generation_id,
                    passed=False, sampleCount=0, editSampleCount=0, toolSampleCount=0, agentTrainingSampleCount=0, diagnosticSampleCount=0,
                    traceRef=trace_ref, traceHash=trace_hash,
                    coverage={'internalContext': 'unknown'}, issues=issues, requestVersions=[], consumedSourceHashes=[], samples=[])
    try:
        archives = _archive_closure(root.name, rows, lambda name: _archive_bytes(root, name))
    except (OSError, ValueError) as error:
        issue('archive_reference', str(error))
        archives = dict(scope='raw_prefix_source_input_archives', complete=False)
    selected = [row for row in rows if generation_id is None or row.get('generationId') == generation_id]

    def load_snapshot(source_hash, ref, event):
        if source_hash in snapshots:
            return snapshots[source_hash]
        try:
            files = _source_files(root, source_hash, ref)
            snapshots[source_hash] = files
            return files
        except (OSError, ValueError, KeyError) as error:
            issue('source_unreadable', str(error), event)
            return None

    link_map, related = _related_events(rows, selected)

    def linked_outcomes(start, initial_links, generation):
        outcomes = _linked_outcomes(related, start, initial_links)
        for event_id in outcomes:
            if event_map[event_id].get('generationId') != generation:
                issue('generation_scope', 'Linked outcome belongs to another generation', event_map[event_id])
        return outcomes

    public_inputs = []
    tool_calls = {}
    paired_calls = set()
    for index, event in enumerate(selected):
        event_type = event.get('type')
        public_input = _public_input(event)
        if public_input:
            public_inputs.append(public_input)
        try:
            _media(root, event)
        except (OSError, ValueError) as error:
            issue('missing_reference', str(error), event)
        if event_type == 'tool_call':
            if not isinstance(event.get('args'), dict) or not event.get('name'):
                issue('invalid_tool_call', 'Actual tool name/parameters are missing', event)
            else:
                tool_calls[event['eventId']] = (event, list(public_inputs))
        elif event_type == 'tool_result':
            pair = tool_calls.get(event.get('callEventId'))
            if pair is None or pair[0]['name'] != event.get('name') or pair[0].get('generationId') != event.get('generationId') or ('result' not in event and 'error' not in event):
                issue('unpaired_tool_result', 'Tool result has no matching actual call', event)
                continue
            call, inputs = pair
            paired_calls.add(call['eventId'])
            returned = event['result'] if 'result' in event else {'isError': True, 'returnedError': event['error']}
            actor, origin = call.get('actor') or 'unknown', call.get('origin') or 'unknown'
            agent_eligible = actor == 'codex-mcp' or (actor == 'unknown' and origin == 'codex-mcp')
            samples.append(dict(kind='tool', eventId=call['eventId'], requirementVersion=call.get('requirementVersion'),
                                actor=actor, origin=origin, agentTrainingEligible=agent_eligible,
                                actorProvenance='local_tool_origin_label_not_authentication',
                                visibleInputRefs=[item['eventId'] for item in inputs], publicInputRefs=[item['eventId'] for item in inputs],
                                action={'name': call['name'], 'args': call['args']}, returned=returned,
                                returnEventId=event['eventId'], outcomeRefs=linked_outcomes(index+1, link_map[call['eventId']] | link_map[event['eventId']], call.get('generationId')),
                                inputCoverage={'internalContext': 'unknown', 'toolParameters': 'captured', 'returnedContent': 'captured'}))
        elif event_type == 'source_snapshot':
            source_hash, ref = event.get('sourceHash'), event.get('snapshotRef')
            after = load_snapshot(source_hash, ref, event)
            previous = event.get('previousSourceHash')
            if not previous or after is None:
                continue
            before_ref = next((row['snapshotRef'] for row in rows if row.get('type') == 'source_snapshot' and row.get('sourceHash') == previous),
                              f'.trace/sources/{previous}.json')
            before = load_snapshot(previous, before_ref, event)
            if before is None or before == after:
                continue
            explicit_refs = event.get('visibleInputRefs', [])
            native_inputs = [item for item in public_inputs if item['sentToModel'] == 'captured']
            for input_ref in explicit_refs:
                if input_ref not in event_map:
                    issue('missing_input', f'Unknown visible input event: {input_ref}', event)
            known = _edit_links(related, index+1, source_hash, link_map)
            outcomes = linked_outcomes(index+1, known, event.get('generationId'))
            if not native_inputs or not outcomes:
                issue('edit_context_gap', 'Recoverable edit lacks captured public input or linked execution outcome', event, 'gap')
            actor, origin = event.get('actor') or 'unknown', event.get('origin') or 'unknown'
            manual_edit = actor in ('manual', 'verification') or event.get('cause') in ('manual_edit', 'debug_build')
            native_action = next((row for row in selected[:index] if event.get('cause') and row.get('type') == 'codex' and
                                  ((row.get('method') == 'item/completed' and row.get('params', {}).get('item', {}).get('type') in ('fileChange', 'commandExecution') and
                                    row.get('params', {}).get('item', {}).get('id') == event['cause']) or
                                   (row.get('method') == 'turn/completed' and row.get('params', {}).get('turn', {}).get('id') == event.get('cause')))), None)
            if not native_action:
                issue('edit_author_gap', 'Source change has no matching native source-action association; retained as diagnostic', event, 'gap')
            if native_action and not manual_edit:
                actor = 'codex-native'
            diffs = {name: ''.join(difflib.unified_diff(before.get(name, '').splitlines(keepends=True), after.get(name, '').splitlines(keepends=True),
                                                      fromfile=f'before/{name}', tofile=f'after/{name}'))
                     for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)}
            samples.append(dict(kind='edit', eventId=event['eventId'], requirementVersion=event.get('requirementVersion'),
                                actor=actor, origin=origin, agentTrainingEligible=bool(native_inputs and outcomes and native_action and not manual_edit),
                                actorProvenance='native_item_link' if native_action else 'source_author_unknown',
                                nativeActionRef=native_action['eventId'] if native_action else None,
                                beforeSourceHash=previous, afterSourceHash=source_hash, beforeSourceRef=before_ref, afterSourceRef=ref,
                                beforeSources=before, afterSources=after, diffs=diffs,
                                visibleInputRefs=explicit_refs or [item['eventId'] for item in native_inputs], publicInputRefs=[item['eventId'] for item in public_inputs],
                                outcomeRefs=outcomes, inputCoverage={'internalContext': 'unknown', 'sourceSnapshots': 'recoverable', 'sourceObservedByModel': 'unknown'}))
    if not samples:
        issue('empty_samples', 'No qualifying editing or tool samples were consumed')
    edits = sum(sample['kind'] == 'edit' for sample in samples)
    agent_samples = sum(sample['agentTrainingEligible'] for sample in samples)
    if not agent_samples:
        issue('no_agent_samples', 'No qualified observable native-input edit or agent-origin tool sample; manual/verification/unknown calls remain diagnostics')
    coverage = dict(internalContext='unknown', privateThoughts='notCaptured', publicInputCount=len(public_inputs),
                    nativeEventCount=sum(row.get('type') == 'codex' for row in selected), sourceSnapshotCount=len(snapshots),
                    openToolCalls=len(set(tool_calls)-paired_calls), traceEventCount=len(selected),
                    actorAttribution='recorded_local_origin_and_native_input_links_not_strong_authentication', archiveClosure=archives,
                    gaps=[item for item in issues if item.get('severity') == 'gap'])
    return dict(schema='sceneagent-generation-consumer-v2', version=2, projectId=root.name, generationId=generation_id,
                passed=bool(agent_samples) and not any(item.get('severity') == 'error' for item in issues),
                traceRef=trace_ref, traceHash=trace_hash,
                sampleCount=len(samples), editSampleCount=edits, toolSampleCount=len(samples)-edits,
                agentTrainingSampleCount=agent_samples, diagnosticSampleCount=len(samples)-agent_samples,
                coverage=coverage, issues=issues, requestVersions=sorted({row['requirementVersion'] for row in selected if 'requirementVersion' in row}),
                consumedSourceHashes=list(snapshots), samples=samples)


def _sample_reader(project_directory, report):
    root = Path(project_directory).resolve()
    if 'schema' not in report:
        if 'version' in report or not isinstance(report.get('samples'), list) or any(
                'outcomeRefs' in sample or 'publicInputRefs' in sample or
                not isinstance(sample.get('outcomes'), list) or not isinstance(sample.get('publicInputs'), list)
                for sample in report['samples']):
            raise ValueError('unrecognized legacy report schema')
        return lambda sample: dict(sample)
    if report.get('schema') != 'sceneagent-generation-consumer-v2' or report.get('version') != 2:
        raise ValueError('unknown generation consumer schema or version')
    if report.get('projectId') != root.name or 'generationId' not in report:
        raise ValueError('report project or generation scope differs')
    digest = report.get('traceHash')
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError('frozen trace hash is missing')
    if _reference_path(root, report.get('traceRef')) == (root/'generation-trace.jsonl').resolve():
        raise ValueError('restoration requires frozen trace, never active trace')
    trace = _reference(root, report.get('traceRef'), digest)
    rows = [json.loads(line) for line in trace.decode('utf8').splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError('trace events must be objects')
    events, positions = {}, {}
    for index, event in enumerate(rows):
        event_id = event.get('eventId')
        if not isinstance(event_id, str) or not event_id.startswith(root.name+':') or event.get('projectId', root.name) != root.name or event_id in events:
            raise ValueError('missing, out-of-project or duplicate event identity')
        events[event_id], positions[event_id] = event, index
    _archive_closure(root.name, rows, lambda name: _archive_bytes(root, name))
    generation = report['generationId']
    selected = [event for event in rows if generation is None or event.get('generationId') == generation]
    selected_positions = {event['eventId']: index for index, event in enumerate(selected)}
    link_map, related = _related_events(rows, selected)
    public_inputs = []
    calls = {}
    for event in selected:
        _media(root, event)
        if event.get('type') == 'source_snapshot':
            source_hash = event.get('sourceHash')
            _source_files(root, source_hash, event.get('snapshotRef'))
        public_input = _public_input(event)
        if public_input:
            public_inputs.append(public_input)
        if event.get('type') == 'tool_call':
            if not event.get('name') or not isinstance(event.get('args'), dict):
                raise ValueError('tool call has no actual name or parameters')
            calls[event['eventId']] = event
        elif event.get('type') == 'tool_result':
            call = calls.get(event.get('callEventId'))
            if call is None or call['name'] != event.get('name') or call.get('generationId') != event.get('generationId') or not ('result' in event or 'error' in event):
                raise ValueError('tool result has no matching actual call in generation')

    def event_ref(ref, allowed_types):
        if isinstance(ref, dict):
            for key in ('traceHash', 'projectId', 'generationId'):
                if key in ref and ref[key] != report.get(key):
                    raise ValueError(f'event reference {key} differs from report scope')
            ref = ref.get('eventId')
        if not isinstance(ref, str) or ref not in events:
            raise ValueError(f'unknown event reference: {ref}')
        event = events[ref]
        if event.get('type') not in allowed_types or generation is not None and event.get('generationId') != generation:
            raise ValueError('event reference has wrong type or generation scope')
        return event

    def referenced_events(sample, field, allowed_types):
        refs = sample.get(field)
        if not isinstance(refs, list):
            raise ValueError(f'missing {field}')
        result = [event_ref(ref, allowed_types) for ref in refs]
        ids = [event['eventId'] for event in result]
        if len(ids) != len(set(ids)) or ids != sorted(ids, key=positions.__getitem__):
            raise ValueError(f'duplicate or unordered {field}')
        return result

    def read(sample, restore=True):
        if sample.get('kind') == 'tool':
            event = event_ref(sample.get('eventId'), ('tool_call',))
            result = event_ref(sample.get('returnEventId'), ('tool_result',))
            if result.get('callEventId') != event['eventId'] or result.get('generationId') != event.get('generationId'):
                raise ValueError('sample tool call/result pairing differs')
            returned = result['result'] if 'result' in result else {'isError': True, 'returnedError': result['error']}
            if sample.get('action') != {'name': event['name'], 'args': event['args']} or sample.get('returned') != returned:
                raise ValueError('sample tool content differs from frozen trace')
            suffix_start = positions[result['eventId']]
            expected_outcomes = _linked_outcomes(related, selected_positions[result['eventId']]+1,
                                                 link_map[event['eventId']] | link_map[result['eventId']])
        elif sample.get('kind') == 'edit':
            event = event_ref(sample.get('eventId'), ('source_snapshot',))
            for input_ref in event.get('visibleInputRefs', []):
                if input_ref not in events:
                    raise ValueError(f'Unknown visible input event: {input_ref}')
            suffix_start = positions[event['eventId']]
            start = selected_positions[event['eventId']]+1
            expected_outcomes = _linked_outcomes(related, start, _edit_links(related, start, event.get('sourceHash'), link_map))
            if sample.get('afterSourceHash') != event.get('sourceHash') or sample.get('beforeSourceHash') != event.get('previousSourceHash'):
                raise ValueError('sample source version differs from frozen event')
            for prefix in ('before', 'after'):
                files = _source_files(root, sample.get(prefix+'SourceHash'), sample.get(prefix+'SourceRef'))
                if files != sample.get(prefix+'Sources'):
                    raise ValueError('sample source snapshot hash or content differs')
        else:
            raise ValueError('unknown sample kind')
        outcomes = referenced_events(sample, 'outcomeRefs', ('operation_started', 'operation_completed', 'operation_failed', 'tool_call', 'tool_result'))
        if [item['eventId'] for item in outcomes] != expected_outcomes:
            raise ValueError('outcome references differ from complete forward-linked events')
        if any(positions[item['eventId']] <= suffix_start or item.get('generationId') != event.get('generationId') for item in outcomes):
            raise ValueError('outcome reference outside sample suffix or generation')
        input_events = referenced_events(sample, 'publicInputRefs', ('user_message', 'requirement_added', 'codex'))
        inputs = [_public_input(item) for item in input_events]
        expected_inputs = [item for item in public_inputs if positions[item['eventId']] <= positions[event['eventId']]]
        if any(item is None for item in inputs) or inputs != expected_inputs:
            raise ValueError('public input references differ from captured full prefix')
        if not restore:
            return
        restored = {key: value for key, value in sample.items() if key not in ('outcomeRefs', 'publicInputRefs')}
        restored.update(outcomes=outcomes, publicInputs=inputs)
        return restored
    for sample in report['samples']:
        read(sample, restore=False)
    return read


def iter_samples(project_directory, report):
    """Restore full training samples lazily from the report's hash-bound frozen trace."""
    read = _sample_reader(project_directory, report)
    for sample in report['samples']:
        yield read(sample)


def read_sample(project_directory, report, sample):
    """Restore one selected sample without expanding the remaining samples."""
    return _sample_reader(project_directory, report)(sample)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_directory')
    parser.add_argument('--generation-id')
    args = parser.parse_args()
    report = consume(args.project_directory, args.generation_id)
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)
