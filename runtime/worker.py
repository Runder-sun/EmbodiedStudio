"""Remote operation entry point; the authoritative source lives in the workspace."""
from __future__ import annotations

import ast
import base64
from dataclasses import asdict
import hashlib
import json
import math
from copy import deepcopy
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runtime.scene import Task, World, revision_key, valid_id
from runtime.records import atomic_json as save, validate_protocol, validate_publication, has_control_ports
from runtime.assets import assets
from runtime.jobs import measure, flush_performance, worker_timeout

save = measure('artifactWrite')(save)

JOB_DIRECTORY = None
NATIVE_DIAGNOSTICS = {'nativeBuildLog':'native-build.log', 'nativeAuditLog':'native-audit.log',
                      'mdlAuditLog':'mdl-audit.log', 'nativeCompileLog':'native-compile.log',
                      'installationLog':'installation.log'}



def native_failure(message, log_path):
    """Expose a bounded real child error while retaining the complete log."""
    try:
        with Path(log_path).open('rb') as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 8192))
            tail = log.read(8192).decode('utf8', errors='replace')[-1200:]
    except OSError as error:
        return message + '; diagnostic log unavailable: ' + str(error)
    return message + '; retained log: ' + str(log_path) + '\nNative diagnostic tail (untrusted output):\n' + tail


def emit(type, **fields):
    event = {'type': type, **fields}
    if JOB_DIRECTORY is not None:
        from runtime.jobs import append_event
        append_event(JOB_DIRECTORY, event)
    else:
        print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)


def progress(stage, message, **fields):
    emit('progress', stage=stage, message=message, **fields)
    flush_performance('progress:' + stage)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def runtime_versions():
    from importlib.metadata import version, PackageNotFoundError
    from runtime.motion import motion_runtime_identity
    result = {'python': sys.version}
    for name in ('isaacsim', 'numpy', 'trimesh', 'imageio', 'imageio-ffmpeg', 'Pillow', 'torch', 'warp-lang', 'nvidia-curobo'):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return {**result, 'motion': motion_runtime_identity()}


def runtime_key():
    directory = Path(__file__).parent
    paths = [*directory.glob('*.py'), *directory.glob('motion-dependencies.json')]
    return revision_key(code={p.name: digest(p) for p in sorted(paths)},
                        engine='isaacsim', physics='physx', libraries=runtime_versions())


def verify_inputs(manifest, current_runtime, directory=None, require_collisions=False):
    execution = manifest.get('runtimeExecution')
    if (not isinstance(execution, dict) or execution.get('python') != sys.executable
            or execution.get('prefix') != sys.prefix or manifest.get('runtimeVersions') != runtime_versions()):
        raise ValueError('冻结环境的解释器或实际依赖身份不匹配，请使用该版本的独立前缀。')
    if manifest['runtime'] != current_runtime:
        raise ValueError('运行内核已变化，需要重新构建场景')
    for filename, expected in manifest['assets'].items():
        path = Path(filename) if Path(filename).is_absolute() else Path(directory) / filename
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f'锁定资产已变化或缺失，需要重新构建场景：{filename}')
    for filename, expected in manifest.get('frozenArtifactDigests', {}).items():
        if digest(Path(directory) / filename) != expected:
            raise ValueError('frozen artifact digest mismatch')
    for filename, expected in manifest.get('sourceDigests', {}).items():
        if digest(Path(directory) / filename) != expected:
            raise ValueError('frozen source digest mismatch')
    from runtime.records import validate_io
    compiled = manifest.get('compiled', {})
    validate_io(manifest.get('io'))
    if not _same_json(manifest['io'], compiled.get('io')):
        raise ValueError('native build I/O differs from its verified compiled binding')
    if compiled['nativeStageAudit'] not in manifest.get('frozenArtifactDigests', {}):
        raise ValueError('native stage audit is not frozen in the manifest')
    audit_path = within(Path(directory), compiled['nativeStageAudit'])
    expected = json.loads(audit_path.read_text())
    if (expected.get('schema') not in ('sceneagent-native-stage-audit-v1', 'sceneagent-native-stage-audit-v2') or expected.get('entry') != compiled['usd']
            or not isinstance(expected.get('files'), dict) or compiled['usd'] not in expected['files']):
        raise ValueError('native stage audit entry or closure is incomplete')
    locked = {**manifest['assets'], **manifest.get('frozenArtifactDigests', {})}
    if any(name not in locked or locked[name] != checksum for name, checksum in expected['files'].items()):
        raise ValueError('native stage closure differs from the verified manifest file digests')
    from runtime.mdl import verify as verify_mdl
    verify_mdl(expected, directory)
    return {audit_path, *(within(Path(directory), name) for name in expected['files'])}



def within(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('artifact path must stay within the data root')
    return path


def audit_native_stage(directory, entry='scene-source.usda', *, timeout=900, _collect_only=False):
    """Passively inspect every authored layer/variant before Kit can activate it."""
    from pxr import Sdf
    directory = Path(directory).resolve()
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise ValueError('native build output contains a symlink')
    pending, visited, assets, mdl_entries = [within(directory, entry)], {}, set(), []
    deferred_parameters = set()
    executable_keys = {'script', 'scriptfile', 'scriptpath', 'python', 'pythoncode', 'sourcecode',
                       'startup', 'startupscript', 'extensions', 'extension', 'enabledextensions'}

    def inspect_value(value, label, references):
        if isinstance(value, Sdf.AssetPath):
            if value.path:
                references.add(value.path)
        elif isinstance(value, (Sdf.Reference, Sdf.Payload)):
            if value.assetPath:
                references.add(value.assetPath)
        elif isinstance(value, dict):
            for key, child in value.items():
                tokens = str(key).lower().replace(':', '.').replace('_', '.').split('.')
                if any(token in executable_keys for token in tokens):
                    raise ValueError('native stage contains an executable script/extension field: '+label+'/'+str(key))
                inspect_value(child, label+'/'+str(key), references)
        elif isinstance(value, (list, tuple, Sdf.AssetPathArray)):
            for child in value:
                inspect_value(child, label, references)
        else:
            # Deletion and ordering markers do not introduce resolvable assets.
            for name in ('explicitItems', 'addedItems', 'prependedItems', 'appendedItems'):
                items = getattr(value, name, None)
                if items:
                    inspect_value(list(items), label, references)

    while pending:
        current = pending.pop()
        if not current.is_file() or not current.resolve().is_relative_to(directory):
            raise ValueError('native stage dependency is missing or outside the frozen closure')
        key = str(current.relative_to(directory))
        if key in visited:
            continue
        layer = Sdf.Layer.FindOrOpen(str(current))
        if layer is None:
            raise ValueError('native stage layer cannot be passively parsed: '+key)
        layer.Reload(force=True)
        references = set(layer.subLayerPaths)
        mdl_references, mdl_parameters, asset_inputs = set(), set(), set()
        inspect_value(layer.customLayerData, key, references)

        def inspect_spec(path):
            spec = layer.GetObjectAtPath(path)
            if spec is None:
                return
            if isinstance(spec, Sdf.AttributeSpec) and spec.name.startswith('inputs:') and isinstance(spec.default, Sdf.AssetPath):
                asset_inputs.add(spec.default.path)
            if isinstance(spec, Sdf.PrimSpec) and 'info:mdl:sourceAsset' in spec.attributes:
                source = spec.attributes['info:mdl:sourceAsset'].default
                if not isinstance(source, Sdf.AssetPath) or not source.path:
                    raise ValueError('MDL sourceAsset must be an explicit nonempty asset')
                identifier = spec.attributes.get('info:mdl:sourceAsset:subIdentifier')
                parameters = []
                for name, attribute in spec.attributes.items():
                    if not name.startswith('inputs:'):
                        continue
                    connection = attribute.GetInfo('connectionPaths')
                    if any(getattr(connection, key, []) for key in ('explicitItems', 'addedItems', 'prependedItems', 'appendedItems')):
                        raise ValueError('connected MDL input graph is not yet qualified; preserve its source')
                    value = attribute.default
                    if isinstance(value, Sdf.AssetPath):
                        parameters.append({'name':name[len('inputs:'):], 'asset':value.path})
                        if identifier and identifier.default:
                            mdl_parameters.add(value.path)
                mdl_entries.append({'layer':key, 'shader':str(spec.path), 'asset':source.path,
                    'subIdentifier':str(identifier.default) if identifier else '', 'parameters':parameters})
                mdl_references.add(source.path)
            kind = str(getattr(spec, 'typeName', '')).lower()
            if any(token in kind for token in ('omnigraph', 'actiongraph', 'scriptnode', 'scripting', 'python')):
                raise ValueError('native stage contains an unqualified executable graph/script: '+key+str(path))
            tokens = str(getattr(spec, 'name', '')).lower().replace(':', '.').replace('_', '.').split('.')
            if any(token in executable_keys for token in tokens):
                raise ValueError('native stage contains an executable property: '+key+str(path))
            name = str(getattr(spec, 'name', ''))
            samples = layer.ListTimeSamplesForPath(path) if isinstance(spec, Sdf.AttributeSpec) else []
            if samples and (name.startswith('xformOp') or name.startswith(('physics:', 'physx', 'state:'))
                    or name in ('points', 'positions', 'velocities', 'orientations', 'translations', 'rotations',
                                'scales', 'blendShapeWeights', 'size', 'radius', 'height', 'extent',
                                'faceVertexCounts', 'faceVertexIndices')):
                raise ValueError('native stage contains an unqualified animated physical source: '+key+str(path))
            for name in spec.ListInfoKeys():
                lowered = str(name).lower()
                if lowered == 'sublayeroffsets':
                    continue  # Layer paths are audited separately; offsets contain no executable values.
                if lowered in executable_keys or lowered.startswith(('omni:graph', 'ogn:')):
                    raise ValueError('native stage contains an executable field: '+key+str(path)+':'+str(name))
                value = spec.GetInfo(name)
                if lowered == 'apischemas':
                    for values in ('explicitItems', 'addedItems', 'prependedItems', 'appendedItems'):
                        if any(any(token in str(item).lower() for token in ('script', 'python', 'omnigraph'))
                               for item in getattr(value, values, [])):
                            raise ValueError('native stage contains executable applied schemas')
                inspect_value(value, key+str(path)+':'+str(name), references)

        layer.Traverse(Sdf.Path.absoluteRootPath, inspect_spec)
        references -= mdl_parameters
        visited[key] = digest(current)
        for reference in references:
            if '://' in reference or reference.startswith('anon:'):
                raise ValueError('native stage closure cannot contain unresolved network/anonymous assets')
            if (reference in mdl_references and Path(reference).suffix.lower() == '.mdl'
                    and '/' not in reference and not reference.startswith('.')):
                # A bare MDL sourceAsset is an official search-path identifier (e.g. GenieSim).
                # Only this authored MDL field may delegate a missing local file to the SDK.
                continue
            target = Path(reference) if Path(reference).is_absolute() else current.parent/reference
            target = target.resolve()
            if not target.is_relative_to(directory):
                raise ValueError('native stage dependency leaves or is missing from its frozen closure: '+reference)
            if not target.is_file():
                if reference in asset_inputs:
                    deferred_parameters.add((key, reference))
                    continue
                raise ValueError('native stage dependency leaves or is missing from its frozen closure: '+reference)
            if target.suffix.lower() == '.usdz':
                raise ValueError('USDZ package contents are not yet qualified for passive audit')
            if target.suffix.lower() == '.mdl' and reference in mdl_references:
                continue
            if target.suffix.lower() in ('.mdl', '.mdle', '.mdr', '.mtlx', '.osl', '.oso'):
                raise ValueError('native shader source transitive closure is not yet qualified: '+reference)
            if target.suffix.lower() in ('.usd', '.usda', '.usdc'):
                pending.append(target)
            else:
                if target.suffix.lower() in ('.py', '.pyc', '.so', '.dll', '.dylib', '.ogn'):
                    raise ValueError('native stage asset cannot activate executable source: '+reference)
                assets.add(target)
    if mdl_entries:
        effective, covered = _effective_mdl_entries(directory, entry)
        if deferred_parameters - covered:
            raise ValueError('unresolved shader asset is not a qualified effective MDL parameter')
        mdl_entries.extend(effective)
        mdl_entries.sort(key=lambda row:json.dumps(row, sort_keys=True))
    elif deferred_parameters:
        raise ValueError('native shader asset dependency is missing')
    files = {**visited, **{str(path.relative_to(directory)): digest(path) for path in sorted(assets)}}
    report = {'schema': 'sceneagent-native-stage-audit-v1', 'entry': entry, 'files': files,
            'executableSources': [],
            'scope': 'passive authored-layer/dependency audit; not physics or task qualification'}
    if _collect_only:
        report['mdlEntries'] = mdl_entries
    elif mdl_entries:
        _audit_materials(directory, report, mdl_entries, timeout)
    return report


def _effective_mdl_entries(directory, entry):
    """Let USD compose stronger overs; preserve the true layer owning each default."""
    from pxr import Sdf, Usd, UsdShade
    stage = Usd.Stage.Open(str(directory/entry))
    entries, covered = [], set()
    def authored_layer(attribute):
        specs = [spec for spec in attribute.GetPropertyStack() if spec.HasInfo('default')]
        if not specs:
            raise ValueError('effective MDL asset has no frozen authored default')
        return str(within(directory, specs[0].layer.realPath).relative_to(directory))
    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
        attribute = prim.GetAttribute('info:mdl:sourceAsset')
        if not attribute:
            continue
        asset = attribute.Get()
        if not isinstance(asset, Sdf.AssetPath) or not asset.path:
            raise ValueError('effective MDL sourceAsset is unavailable')
        parameters = []
        for input_ in UsdShade.Shader(prim).GetInputs():
            attr = input_.GetAttr()
            if attr.GetConnections():
                raise ValueError('connected MDL input graph is not yet qualified; preserve its source')
            if attr.GetNumTimeSamples():
                raise ValueError('time-sampled MDL inputs are not yet qualified')
            value = input_.Get()
            if isinstance(value, Sdf.AssetPathArray):
                raise ValueError('MDL asset parameter arrays are not yet qualified')
            if isinstance(value, Sdf.AssetPath):
                layer = authored_layer(attr)
                parameters.append({'name':str(input_.GetBaseName()),'asset':value.path,'layer':layer})
                covered.add((layer, value.path))
        entries.append({'layer':authored_layer(attribute), 'shader':str(prim.GetPath()),
            'effectivePrimPath':str(prim.GetPath()), 'asset':asset.path,
            'subIdentifier':str(prim.GetAttribute('info:mdl:sourceAsset:subIdentifier').Get() or ''),
            'parameters':parameters})
    return entries, covered


def _audit_materials(directory, report, entries, timeout):
    from runtime import mdl
    material = mdl.audit(directory, entries, controller_command('--mdl-audit'), timeout)
    for key, expected in material['files'].items():
        if key.startswith('package/'):
            name = key[len('package/'):]
            if digest(within(directory, name)) != expected:
                raise ValueError('MDL dependency changed during official audit')
            report['files'][name] = expected
    report.update(schema='sceneagent-native-stage-audit-v2', mdl=material)
    mdl.verify(report, directory)


def isolated_native_audit(directory, *, timeout=900):
    """Keep passive USD imports out of the future GPU Kit process."""
    directory = Path(directory).resolve()
    with tempfile.TemporaryDirectory(prefix='sceneagent-native-audit-') as name:
        output = Path(name)
        command = controller_command('--native-stage-audit', resources=directory)
        command = [*command[:-3], '--bind', str(output), '/audit-output', *command[-3:]]
        with (directory/'native-audit.log').open('wb') as log:
            result = subprocess.run(command, input=b'{}', stdout=log, stderr=subprocess.STDOUT,
                                    timeout=timeout, close_fds=True)
        if result.returncode or not (output/'result.json').is_file():
            raise ValueError(native_failure('isolated passive native audit failed', directory/'native-audit.log'))
        report = json.loads((output/'result.json').read_text())
    entries = report.pop('mdlEntries')
    if entries:
        _audit_materials(directory, report, entries, timeout)
    return report


def program(source, name, *, filename=None):
    scope = {'__name__': name}
    if filename is not None:
        scope['__file__'] = str(Path(filename).resolve())
    exec(compile(source, scope.get('__file__', name), 'exec'), scope)
    return scope


def artifact_paths(root, directory, files):
    return {label: str((directory / filename).relative_to(root)) for label, filename in files.items()}


def source_files(payload):
    files = payload.get('sourceFiles') or {name + '.py': code for name, code in payload['sources'].items()}
    for name in files:
        path = Path(name)
        if (path.is_absolute() or '..' in path.parts or '\\' in name or path.as_posix() != name
                or not (path.suffix == '.py' or name.endswith('.robot.json'))):
            raise ValueError('source snapshots require relative Python or public robot profile files')
        if name.endswith('.robot.json'):
            from runtime.robot import parse_profile
            parse_profile(files[name])
    for required in ('scene.py', 'tasks.py'):
        if required not in files:
            raise ValueError(f'missing source: {required}')
    if 'environmentSourceRefs' in payload:
        refs = payload['environmentSourceRefs']
        if (not isinstance(refs, list) or any(not isinstance(name, str) for name in refs)
                or len(set(refs)) != len(refs) or not {'scene.py', 'tasks.py'}.issubset(refs)
                or 'expert.py' in refs or any(name not in files for name in refs)):
            raise ValueError('environmentSourceRefs requires the complete independent environment source closure')
        for entry in ('scene.py', 'tasks.py'):
            source_closure({'sourceRef': entry, 'helperRefs': [name for name in refs if name != entry]}, files)
        files = {name: files[name] for name in refs}
    return files


def import_locks(files):
    """Record actual declared imports; project imports must have frozen source files."""
    declared = set()
    for name, source in files.items():
        if name.endswith('.robot.json'):
            from runtime.robot import parse_profile
            parse_profile(source)
            continue
        for node in ast.walk(ast.parse(source, name)):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom):
                base = list(Path(name).parent.parts) if node.level else []
                if node.level:
                    if node.level > len(base)+1:
                        raise ValueError('relative import leaves frozen project')
                    base = base[:len(base)-node.level+1]
                module = '.'.join([*base, *([node.module] if node.module else [])])
                modules = [module] if module else []
            for module in modules:
                declared.add(module)
                # Availability is determined by imports in the isolated locked prefix.
                # A hand-maintained root-name list is neither dependency discovery nor a sandbox.
    return sorted(declared)


def source_closure(declaration, files, source_digests=None):
    """Select exactly the frozen entry and declared project helpers."""
    from runtime.scene import validate_source_declaration
    names = validate_source_declaration(declaration)
    if any(name not in files for name in names):
        raise ValueError('declared source is absent from the frozen project')
    selected = {name: files[name] for name in names}
    hashes = {name: hashlib.sha256(source.encode()).hexdigest() for name, source in selected.items()}
    if source_digests is not None and any(source_digests.get(name) != value for name, value in hashes.items()):
        raise ValueError('frozen source digest mismatch')
    import_locks(selected)
    project_roots = {name.split('/')[0].removesuffix('.py') for name in files if name.endswith('.py')}
    for name, source in selected.items():
        if name.endswith('.robot.json'):
            continue
        for node in ast.walk(ast.parse(source, name)):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom):
                base = list(Path(name).parent.parts)
                if node.level:
                    if node.level > len(base)+1:
                        raise ValueError('relative import leaves its frozen closure')
                    base = base[:len(base)-node.level+1]
                else:
                    base = []
                module = '.'.join([*base, *([node.module] if node.module else [])])
                modules = [module, *[module+'.'+alias.name for alias in node.names]]
            for module in modules:
                if module.split('.')[0] not in project_roots:
                    continue
                path = module.replace('.', '/')
                candidates = [path+'.py', path+'/__init__.py']
                candidates += ['/'.join(path.split('/')[:index])+'/__init__.py'
                               for index in range(1, len(path.split('/')))]
                if any(candidate in files and candidate not in selected for candidate in candidates):
                    raise ValueError('source imports an undeclared frozen helper')
    entry = declaration['sourceRef']
    return {'sourceRef': entry, 'sourceDigest': hashes[entry],
            'helperDigests': {name: hashes[name] for name in declaration['helperRefs']}, 'sourceFiles': selected}


def rubric_sources(task, files, source_digests=None):
    result = source_closure(task.rubric, files, source_digests)
    controllers = {case['sourceRef'] for case in task.evaluation['cases']}
    if controllers.intersection(result['sourceFiles']):
        raise ValueError('rubric cannot import an evaluation controller')
    return result


def evaluation_sources(task, files, source_digests=None):
    task.validate_evaluation()
    entries = {case['sourceRef'] for case in task.evaluation['cases']}
    result = {}
    for case in task.evaluation['cases']:
        selected = source_closure({key: case[key] for key in ('sourceRef', 'helperRefs')}, files, source_digests)
        forbidden = (entries-{case['sourceRef']}) | {task.rubric['sourceRef']}
        if forbidden.intersection(selected['sourceFiles']):
            raise ValueError('evaluation controller cannot import grading or another case entry')
        result[case['id']] = selected
    return result


def expert_sources(task, files, source_digests):
    """Freeze the expert's actual static local imports, never the whole project."""
    forbidden = {'scene.py', 'tasks.py', task.rubric['sourceRef']} | {case['sourceRef'] for case in task.evaluation['cases']}
    pending, included = ['expert.py'], set()
    while pending:
        name = pending.pop()
        if name in included:
            continue
        if name in forbidden or name not in files:
            raise ValueError('expert imports a private grading/case entry or missing project source')
        included.add(name)
        for node in ast.walk(ast.parse(files[name], name)):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom):
                base = list(Path(name).parent.parts) if node.level else []
                if node.level:
                    if node.level > len(base)+1:
                        raise ValueError('expert relative import leaves its frozen source closure')
                    base = base[:len(base)-node.level+1]
                module = '.'.join([*base, *([node.module] if node.module else [])])
                modules = [module, *[module+'.'+alias.name for alias in node.names]]
            for module in modules:
                path = module.replace('.', '/')
                candidates = [path+'.py', path+'/__init__.py']
                candidates += ['/'.join(path.split('/')[:index])+'/__init__.py' for index in range(1, len(path.split('/')))]
                pending.extend(candidate for candidate in candidates if candidate in files and candidate not in included)
    # This suffix is the public robot configuration contract, never rubric private data.
    included.update(name for name in files if name.endswith('.robot.json'))
    selected = {name: files[name] for name in sorted(included)}
    import_locks(selected)
    hashes = {name: hashlib.sha256(source.encode()).hexdigest() for name, source in selected.items()}
    if any(source_digests.get(name) != value for name, value in hashes.items()):
        raise ValueError('expert source differs from its frozen digest')
    return {'sourceRef': 'expert.py', 'sourceDigest': hashes['expert.py'],
            'helperDigests': {name: hashes[name] for name in selected if name != 'expert.py'}, 'sourceFiles': selected}


@measure('freeze')
def copy_asset_dependencies(path, directory):
    """Freeze the complete visual/material package and its preparation evidence."""
    from runtime.assets import dependency_paths
    path = path.resolve()
    files = set(dependency_paths(path))
    report_path = path.parent / 'report.json'
    if report_path.exists():
        report = json.loads(report_path.read_text())
        files.add(report_path)
        files.update((path.parent / name).resolve() for name in report.get('preparedFiles', []))
    for current in sorted(files):
        if not current.is_relative_to(path.parent):
            raise ValueError('asset references outside its declared directory')
        if not current.is_file():
            raise FileNotFoundError(current)
        relative = current.relative_to(path.parent)
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, target)
    return directory / path.name


def native_build_entry():
    """Untrusted authoring phase. No generated Python remains in the native runtime."""
    import numpy as np
    from runtime.simulation import (_initialize_usd_schemas, author_native_helpers,
        compile_recording_cameras, _write_camera, camera_for)
    _initialize_usd_schemas()
    from pxr import Usd, UsdGeom
    request = json.load(sys.stdin)
    root = Path('/output')
    os.chdir(root)
    if 'robotSetup' in request:
        try:
            _robot_setup_entry(request, root)
        except Exception as exc:
            save(root/'installation-failure.json', {'error':type(exc).__name__+': '+str(exc), 'traceback':traceback.format_exc()})
            raise
        return
    random.seed(request['seed'])
    np.random.seed(request['seed'])
    world = World(request['seed'], native=True)
    world.stage = Usd.Stage.CreateNew(str(root/'scene-source.usda'))
    world.stage.SetDefaultPrim(UsdGeom.Xform.Define(world.stage, '/World').GetPrim())
    UsdGeom.SetStageMetersPerUnit(world.stage, 1)
    UsdGeom.SetStageUpAxis(world.stage, UsdGeom.Tokens.z)
    world.resources = deepcopy(request['resources'])
    world.source_root = Path('/inputs')
    sys.path.insert(0, '/inputs')
    program(Path('/inputs/scene.py').read_text(), 'scene', filename='/inputs/scene.py')['build'](world)
    for node in world.nodes.values():
        for field, category in (('path', 'assets'), ('material', 'materials')):
            if (field == 'path' and node['kind'] != 'asset') or not node.get(field):
                continue
            source = Path(node[field]).resolve()
            locked = copy_asset_dependencies(source, root/'dependencies'/category/node['id'])
            node[field] = str(locked)
    helper_output = author_native_helpers(world, world.stage, root)
    tasks = program(Path('/inputs/tasks.py').read_text(), 'tasks', filename='/inputs/tasks.py')['build'](world)
    if not isinstance(tasks, (list, tuple)) or not tasks or any(not isinstance(task, Task) for task in tasks):
        raise ValueError('three-product construction requires generated Task declarations')
    for task in tasks:
        task.bind(world)
    validation = world.validate()
    camera = _write_camera(world.stage, camera_for(world, stage=world.stage))
    cameras = {task.id: compile_recording_cameras(world.stage, world, task) for task in tasks}
    world.stage.GetRootLayer().Save()
    for node in world.nodes.values():
        for field in ('path', 'material'):
            if node.get(field):
                node[field] = str(Path(node[field]).relative_to(root))
    save(root/'scene.json', world.snapshot())
    save(root/'tasks.json', [asdict(task) for task in tasks])
    save(root/'native-build.json', {'validation': validation, 'camera': camera,
        'recordingCameras': cameras, 'helpers': helper_output})


def freeze_resources(root, references, directory):
    """Reuse prepared bundles. Their entry can be nested; reportPath owns the package."""
    from runtime.assets import _cache_valid
    if not isinstance(references, list) or any(not isinstance(ref, str) for ref in references):
        raise ValueError('resourceRefs must contain prepared report paths')
    result = {}
    for reference in references:
        report_path = within(root, reference)
        if not report_path.is_relative_to(root/'assets') or report_path.name != 'report.json':
            raise ValueError('resourceRefs require prepared assets/report.json')
        report = json.loads(report_path.read_text())
        if report.get('kind') != 'native_files' or not _cache_valid(report_path.parent):
            raise ValueError('native resource is not a verified prepared file closure')
        name = digest(report_path)
        destination = directory/'dependencies/resources'/name
        if not destination.exists():
            shutil.copytree(report_path.parent, destination, symlinks=True)
        result[reference] = str((destination/report['entry']).relative_to(directory))
    return result


def native_build_command(inputs, directory, root, legacy):
    """Mount one construction output and its immutable source/resource inputs."""
    command = controller_command('--native-build-source', gpu=True)
    command = command[:-3]+['--bind', str(directory), '/output', '--ro-bind', str(inputs), '/inputs',
        '--ro-bind', str(inputs), '/output/build-inputs']+command[-3:]
    resources = directory/'dependencies/resources'
    if resources.is_dir():
        command = command[:-3]+['--ro-bind', str(resources), '/output/dependencies/resources']+command[-3:]
    # Only public asset libraries accompany the declared source/resource inputs.
    for path in (root/'assets', legacy/'assets', legacy/'data'):
        if path.is_dir():
            command = command[:-3]+['--ro-bind', str(path), str(path)]+command[-3:]
    return command


def isolated_native_compile(world, directory, io_declarations, *, recording_cameras=None, camera=None):
    """A moving MDL installation must release its SDK before the atomic rename."""
    with tempfile.TemporaryDirectory(prefix='sceneagent-native-compile-') as name:
        output = Path(name)
        command = controller_command('--native-compile', resources=directory, gpu=True)
        command = [*command[:-3], '--bind', str(output), '/compile-output', *command[-3:]]
        request = {'world':world.snapshot(), 'io':io_declarations,
                   'recordingCameras':recording_cameras, 'camera':camera}
        with (Path(directory)/'native-compile.log').open('wb') as log:
            completed = subprocess.run(command, input=json.dumps(request).encode(),
                stdout=log, stderr=subprocess.STDOUT, close_fds=True)
        if completed.returncode or not (output/'compiled.json').is_file():
            raise ValueError(native_failure('isolated native interface compilation failed', Path(directory)/'native-compile.log'))
        return json.loads((output/'compiled.json').read_text())


def native_compile_entry():
    from runtime import simulation
    request = json.load(sys.stdin)
    try:
        result = simulation.compile_native_world(World.restore(request['world']), '/resources', request['io'],
            recording_cameras=request['recordingCameras'], camera=request['camera'],
            _output_directory='/compile-output')
        save(Path('/compile-output/compiled.json'), result)
        simulation.finish()
    finally:
        simulation.shutdown(0 if Path('/compile-output/compiled.json').is_file() else 7)


@measure('build')
def build(payload, root):
    from runtime.simulation import compile_native_world, export_preview
    files = source_files(payload)
    declared_imports = import_locks(files)
    project_id, seed = valid_id(payload['projectId']), int(payload.get('seed', 0))
    revisions = root/'projects'/project_id/'revisions'
    revisions.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='building-', dir=revisions))
    try:
        inputs = temporary/'build-inputs'
        inputs.mkdir()
        for name, source in files.items():
            path = inputs/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        resources = freeze_resources(root, payload.get('resourceRefs', []), temporary)
        resource_hashes = {str(path.relative_to(temporary)): digest(path)
            for path in (temporary/'dependencies/resources').rglob('*') if path.is_file()}
        command = native_build_command(inputs, temporary, root, Path(payload.get('legacyRoot', '')).resolve())
        progress('program', '正在隔离进程中构建原生场景与任务')
        with (temporary/'native-build.log').open('wb') as log:
            with measure('sourceBuild'):
                completed = subprocess.run(command, input=json.dumps({'seed': seed, 'resources': resources}).encode(),
                    stdout=log, stderr=subprocess.STDOUT, close_fds=True)
        if completed.returncode:
            raise RuntimeError(native_failure('isolated native construction failed', temporary/'native-build.log'))
        if any(not (inputs/name).is_file() or (inputs/name).read_text() != source for name, source in files.items()):
            raise ValueError('native construction changed its frozen source inputs')
        if any(not (temporary/name).is_file() or digest(temporary/name) != expected
               for name, expected in resource_hashes.items()):
            raise ValueError('native construction changed its frozen resource inputs')
        # bwrap's private PID namespace has ended before Sdf or Kit touches the output.
        shutil.rmtree(inputs)
        with measure('nativeAudit'):
            audit = isolated_native_audit(temporary, timeout=worker_timeout(payload))
        save(temporary/'native-stage-audit.json', audit)
        snapshot = json.loads((temporary/'scene.json').read_text())
        world = World.restore(snapshot)
        if not world.native:
            raise ValueError('native builder did not produce a semantic native World')
        validation = world.validate()
        tasks = [Task(**row) for row in json.loads((temporary/'tasks.json').read_text())]
        if not tasks:
            raise ValueError('native build requires generated tasks')
        for task in tasks:
            task.bind(world)
            rubric_sources(task, files)
            evaluation_sources(task, files)
        authored = json.loads((temporary/'native-build.json').read_text())
        progress('geometry', '原生依赖审核通过，正在新进程环境中核验实际接口')
        with measure('nativeBinding'):
            compiled = compile_native_world(world, temporary, world.io,
                recording_cameras=authored['recordingCameras'], camera=authored['camera'])
        compiled['nativeStageAudit'] = 'native-stage-audit.json'
        compiled['rendering'] = {'renderer': 'RealTimePathTracing', 'nativeFrozenStage': True,
            'lightingPixelParity': False, 'fullBSDFVerified': False}
        compiled['physics'] = {'engine': 'isaacsim', 'physics': 'physx', **compiled['io']['clock'],
            'source': 'frozen native USD; runtime initialization recorded separately'}
        preview_error = None
        try:
            export_preview(temporary/compiled['usd'], temporary/'preview.glb', compiled['bindings'])
        except (ValueError, TypeError, KeyError, RuntimeError, TimeoutError) as exc:
            preview_error = f'{type(exc).__name__}: {exc}'
            for name in ('preview.glb', 'preview-raw.glb', 'preview-evidence.json', 'preview-source.usda'):
                (temporary/name).unlink(missing_ok=True)
            progress('preview_unavailable', '原生场景可继续使用；网页预览暂不可用：'+preview_error)
        compiled['rendering']['webPreview'] = {'available': preview_error is None,
            **({'reason': preview_error} if preview_error else {})}
        for name, source in files.items():
            path = temporary/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        contracts = [asdict(task) for task in tasks]
        frozen_names = {'scene.json', 'tasks.json', compiled['usd'], 'native-stage-audit.json'}
        if preview_error is None:
            frozen_names.add('preview.glb')
        assets_locked = {str(path.relative_to(temporary)): digest(path) for path in sorted(temporary.rglob('*'))
            if path.is_file() and str(path.relative_to(temporary)) not in set(files)|frozen_names|set(NATIVE_DIAGNOSTICS.values())}
        runtime_hash = runtime_key()
        revision = revision_key(sourceFiles=files, assets=assets_locked, runtime=runtime_hash, seed=seed,
            nativeStage=audit, io=compiled['io'])
        directory = revisions/revision
        if (directory/'manifest.json').exists():
            existing = json.loads((directory/'manifest.json').read_text())
            if existing.get('status') != 'completed':
                raise ValueError('same revision already contains an unpublished candidate')
            diagnostics = revisions.parent/'build-diagnostics'
            diagnostics.mkdir(exist_ok=True)
            for name in NATIVE_DIAGNOSTICS.values():
                if (temporary/name).is_file():
                    log_path = diagnostics/(temporary.name+'-'+name)
                    (temporary/name).rename(log_path)
                    progress('build_reused', '复用相同冻结版本，本次诊断日志已保留',
                        revision=revision, artifactRef=str(log_path.relative_to(root)))
            shutil.rmtree(temporary)
            return existing
        artifacts = artifact_paths(root, directory, {'scene': 'scene.json',
            'model': compiled['usd'], 'tasks': 'tasks.json', 'manifest': 'manifest.json'})
        if preview_error is None:
            artifacts['preview'] = str((directory/'preview.glb').relative_to(root))
        artifacts['rubrics'] = {task.id: {name: str((directory/name).relative_to(root))
            for name in rubric_sources(task, files)['sourceFiles']} for task in tasks}
        manifest = {'schema': 'sceneagent-build-v6', 'engine': 'isaacsim', 'physics': 'physx',
            'status': 'candidate', 'projectId': project_id, 'projectName': payload.get('projectName', project_id),
            'revision': revision, 'seed': seed, 'runtime': runtime_hash,
            'capabilities': {'stageSplit': True, 'independentBehavior': True, 'robotSetup': True, 'adjustableRubricBudget': True},
            'episodeLimits': {'tasks': {task.id: task.execution for task in tasks}, 'reset': 'frozen_seed_only'},
            'compiled': compiled, 'io': compiled['io'], 'resources': resources,
            'frozenArtifactDigests': {name: digest(temporary/name) for name in sorted(frozen_names)},
            'assets': assets_locked, 'sourceDigests': {name: digest(temporary/name) for name in files},
            'diagnostics': {label:{'artifactRef':str((directory/name).relative_to(root)), 'sha256':digest(temporary/name)}
                            for label,name in NATIVE_DIAGNOSTICS.items() if (temporary/name).is_file()},
            'declaredImports': declared_imports, 'tasks': contracts, 'nodes': len(world.nodes),
            'validation': validation, 'camera': compiled['camera'], 'initialState': compiled['initialState'],
            'coordinateSpec': {'units': 'meters', 'upAxis': 'Z', 'quaternion': 'wxyz'},
            'runtimeSnapshotRef': str(Path(__file__).resolve().parent.parent.relative_to(root)),
            'runtimeVersions': runtime_versions(), 'runtimeExecution': {'python': sys.executable, 'prefix': sys.prefix,
                'workerTimeoutParameter': 'workerTimeoutSeconds', 'controllerOutputContract': 'separate-logs-v1'},
            'rendering': compiled['rendering'], 'physicsSpec': compiled['physics'],
            'terminationMode': 'official_fast_shutdown', 'fullVendorCleanupVerified': False,
            'artifacts': artifacts, 'limitations': ['原生接口核验不代表具体机器人任务、碰撞精度或任务语义已通过。',
                '任务评分由冻结生成rubric定义；未知原生读口不自动获得可信资格。']}
        if preview_error:
            manifest['limitations'].append('网页预览不可用；须通过原生观察检查实际场景：'+preview_error)
        save(temporary/'manifest.json', manifest)
        temporary.rename(directory)
        progress('ready', '原生场景、任务和实际接口已冻结', revision=revision)
        return manifest
    except BaseException as exc:
        save(temporary/'failure.json', {'error': f'{type(exc).__name__}: {exc}', 'traceback': traceback.format_exc()})
        raise


def frozen(root, payload):
    project = root / 'projects' / valid_id(payload['projectId'])
    revision = payload.get('revision')
    if not revision:
        revision = json.loads((project / 'latest.json').read_text())['revision']
    directory = within(project / 'revisions', revision)
    manifest = json.loads((directory / 'manifest.json').read_text())
    validate_protocol(manifest, 'sceneagent-build-v6')
    if manifest.get('status') != 'completed':
        raise ValueError('frozen build was not published after verified worker exit')
    validate_publication(manifest, root)
    verify_inputs(manifest, runtime_key(), directory)
    world = World.restore(json.loads((directory / 'scene.json').read_text()))
    return project, directory, manifest, world



def load_environment(directory, manifest, world, output, task, record=True, camera=None, *, episode_id=None, rubric_session=None, cameras=None):
    from runtime.simulation import Environment
    camera = camera if camera is not None or cameras is not None else (manifest.get('camera') if not record else None)
    env = Environment(deepcopy(manifest['compiled']), world, output, progress, camera, record, task,
        source_directory=directory, episode_id=episode_id, rubric_session=rubric_session, cameras=cameras)
    env.resource_refs = deepcopy(manifest.get('resources', {}))
    return env


def controller_command(entry='--controller', *, sensors=None, resources=None, base_resources=None, arrays=None, gpu=False):
    """Only Python/runtime dependencies are mounted; project/result/private host state are absent."""
    if not shutil.which('bwrap'):
        raise RuntimeError('formal execution requires bubblewrap on the compute host')
    snapshot = Path(__file__).resolve().parent.parent
    prefix = Path(sys.prefix).resolve()
    executable = Path(sys.executable).resolve()
    base = Path(sys.base_prefix).resolve()
    mounts = [Path('/usr'), Path('/bin'), Path('/lib'), Path('/lib64'), prefix, base, snapshot]
    selected = []
    for path in sorted(set(mounts), key=lambda path: len(path.parts)):
        if path.exists() and not any(path.is_relative_to(existing) for existing in selected):
            selected.append(path)
    command = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--proc', '/proc',
               '--dev', '/dev', '--tmpfs', '/tmp', '--chdir', '/tmp', '--clearenv',
               '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'PYTHONNOUSERSITE', '1',
               '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
               '--setenv', 'HOME', '/tmp/home', '--setenv', 'XDG_CACHE_HOME', '/tmp/xdg-cache',
               '--setenv', 'XDG_CONFIG_HOME', '/tmp/config', '--setenv', 'XDG_DATA_HOME', '/tmp/data',
               '--setenv', 'WARP_CACHE_PATH', '/tmp/warp', '--setenv', 'TORCH_EXTENSIONS_DIR', '/tmp/torch',
               '--setenv', 'CUDA_CACHE_PATH', '/tmp/cuda-cache']
    for path in selected:
        command += ['--ro-bind', str(path), str(path)]
    from runtime.simulation import _cpu_environment
    abi = _cpu_environment()
    for name in ('PYTHONPATH', 'LD_LIBRARY_PATH', 'PXR_PLUGINPATH_NAME'):
        command += ['--setenv', name, abi[name]]
    if sensors is not None:
        sensors = Path(sensors)
        if sensors.is_symlink() or not sensors.is_dir():
            raise ValueError('controller sensor mount must be a plain committed directory')
        command += ['--ro-bind', str(sensors.resolve()), '/sensors']
    for path, destination in ((resources, '/resources'), (base_resources, '/base-resources'),
                              (arrays, str(Path(arrays).resolve()) if arrays else None)):
        if path is not None:
            path = Path(path)
            if path.is_symlink() or not path.is_dir():
                raise ValueError('declared input mount must be a plain frozen directory')
            command += ['--ro-bind', str(path.resolve()), destination]
    if gpu:
        index, visible = os.environ.get('SCENEAGENT_GPU'), os.environ.get('CUDA_VISIBLE_DEVICES')
        if not index or not index.isdecimal() or not visible or ',' in visible:
            raise ValueError('isolated GPU compute requires the job-assigned single GPU')
        for device in ('/dev/nvidia'+index, '/dev/nvidiactl', '/dev/nvidia-uvm', '/dev/nvidia-uvm-tools'):
            if Path(device).exists():
                command += ['--dev-bind', device, device]
        if Path('/sys').exists():
            command += ['--ro-bind', '/sys', '/sys']
        command += ['--setenv', 'CUDA_VISIBLE_DEVICES', visible, '--setenv', 'CUDA_DEVICE_ORDER', 'PCI_BUS_ID',
                    '--setenv', 'SCENEAGENT_GPU', index, '--setenv', 'OMNI_KIT_ACCEPT_EULA', 'YES']
    if Path('/etc/ld.so.cache').exists():
        command += ['--ro-bind', '/etc/ld.so.cache', '/etc/ld.so.cache']
    return command + [sys.executable, str(Path(__file__).resolve()), entry]


def asset_source_entry():
    """Execute frozen asset source; data outputs and the return declaration are separate."""
    import numpy as np
    setup = json.load(sys.stdin)
    sources = Path('/inputs/sources')
    sys.path.insert(0, str(sources))
    random.seed(0)
    np.random.seed(0)
    scope = program((sources / setup['sourceFile']).read_text(), setup['sourceFile'])
    asset = scope['build_asset'](setup['materials'])
    if isinstance(asset, dict):
        result = {'kind': 'native_files', 'output': asset}
    else:
        import trimesh
        if not isinstance(asset, (trimesh.Scene, trimesh.Trimesh)):
            raise ValueError('build_asset(materials) must return a Trimesh, Scene or native entry/files declaration')
        asset.export('/output/visual.glb', file_type='glb')
        result = {'kind': 'static_mesh'}
    Path('/result/output.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False))


def generate_asset(payload, legacy, root):
    from runtime.assets import prepare, generated_cached_report, prepare_generated, prepare_generated_native
    files = source_files(payload)
    source_file = payload['sourceFile']
    if source_file not in files:
        raise ValueError('generator source must be in the project snapshot')
    source_hash = hashlib.sha256(json.dumps(files, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if source_hash != payload.get('expectedSourceHash') or source_hash != payload.get('sourceHash'):
        raise ValueError('generator source hash changed')
    import_locks(files)
    material_reports = {}
    for id in payload.get('materialIds', []):
        report = prepare(legacy, root, id)
        if report.get('kind') != 'material':
            raise ValueError('generator inputs must be prepared material IDs')
        material_reports[id] = report
    cached = generated_cached_report(root, files, source_file, list(material_reports.values()))
    if cached is not None:
        return cached
    parent = root / 'assets' / 'generated'
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='generating-', dir=parent))
    inputs, output, result_directory = staging / 'inputs', staging / 'output', staging / 'result'
    sources = inputs / 'sources'
    output.mkdir()
    result_directory.mkdir()
    for name, source in files.items():
        path = sources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    materials = {}
    for index, (id, report) in enumerate(material_reports.items()):
        destination = inputs / 'materials' / str(index)
        locked = copy_asset_dependencies(Path(report['reportPath']), destination)
        frozen_report = json.loads(locked.read_text())
        frozen_report['reportPath'] = f'/inputs/materials/{index}/{locked.name}'
        frozen_report['maps'] = {key: f'/inputs/materials/{index}/{name}'
                                 for key, name in frozen_report.get('maps', {}).items()}
        materials[id] = frozen_report
    command = controller_command('--asset-source')
    command[-3:-3] = ['--ro-bind', str(inputs), '/inputs', '--bind', str(output), '/output',
                       '--bind', str(result_directory), '/result']
    try:
        progress('asset_generator', '正在执行冻结资产程序与声明材料')
        child = subprocess.run(command, input=json.dumps({'sourceFile': source_file, 'materials': materials}),
                               text=True, capture_output=True, timeout=90, close_fds=True)
        if child.returncode:
            raise RuntimeError('isolated asset generator failed: ' + child.stderr[-4000:])
        result_path = result_directory / 'output.json'
        if result_path.is_symlink() or not result_path.is_file() or result_path.stat().st_size > 1024 * 1024:
            raise ValueError('isolated asset output declaration missing or invalid')
        result = json.loads(result_path.read_text())
        options = {'name': payload.get('name', ''), 'project_id': payload.get('projectId')}
        if isinstance(result, dict) and result.get('kind') == 'native_files' and set(result) == {'kind', 'output'}:
            report = prepare_generated_native(root, output, result['output'], files, source_file,
                                              list(material_reports.values()), **options)
        elif result == {'kind': 'static_mesh'}:
            report = prepare_generated(root, output / 'visual.glb', files, source_file,
                                       list(material_reports.values()), **options)
        else:
            raise ValueError('invalid isolated asset output declaration')
        shutil.rmtree(staging)
        return report
    except BaseException as error:
        save(staging / 'failure.json', {'error': str(error), 'sourceHash': source_hash,
                                      'sourceFile': source_file, 'materialIds': list(material_reports)})
        raise


def publisher_preview(metadata, package, root, checksums=None):
    """Read a pinned, per-object publisher image; this never renders or qualifies an asset."""
    from runtime.assets import _collection_path
    package, root = package.resolve(), root.resolve()
    if not package.is_relative_to(root/'assets'):
        return {}
    package = _collection_path(root/'assets', str(package.relative_to(root/'assets')))
    for preview in metadata.get('qualityEvidence', {}).get('publisherPreviews', []):
        if preview.get('scope') != 'per_object' or preview.get('type') != 'publisher_render':
            continue
        name = preview.get('file')
        if not isinstance(name, str) or checksums is not None and name not in checksums:
            continue
        pins = [preview.get('sha256'), checksums.get(name) if checksums is not None else None]
        pins = [pin for pin in pins if isinstance(pin, str) and len(pin) == 64 and all(c in '0123456789abcdef' for c in pin)]
        if not pins:
            continue
        image = _collection_path(package, name)
        if not image.is_file():
            continue
        mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}.get(image.suffix.lower())
        if not mime or image.stat().st_size > 2*1024*1024:
            continue
        content = image.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if any(actual != pin for pin in pins):
            raise ValueError('publisher preview source changed or differs from its frozen digest')
        return {'preview': {'kind': 'publisher_render', 'file': name, 'url': preview.get('url'),
                            'sha256': actual, 'systemRendered': False, 'status': 'available'},
                'image': {'data': base64.b64encode(content).decode(), 'mimeType': mime}}
    return {}


def publish_asset_result(report, payload, root):
    """Expose small evidence inside this project; shared asset files stay in the cache."""
    if not payload.get('projectId'):
        return report
    project = root / 'projects' / valid_id(payload['projectId'])
    key = hashlib.sha256(str(report.get('reportPath') or report.get('path')).encode()).hexdigest()[:24]
    output = project / 'asset-previews' / key
    output.mkdir(parents=True, exist_ok=True)
    save(output / 'report.json', report)
    result = {**report, 'artifactRef': str((output / 'report.json').relative_to(root))}
    thumbnail = report.get('thumbnailPath')
    if thumbnail:
        path = Path(thumbnail)
        if not path.is_absolute():
            path = Path(report['reportPath']).parent / path
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('prepared thumbnail must be in the configured data directory')
        image = output / 'thumbnail.png'
        shutil.copy2(path, image)
        result.update(thumbnailRef=str(image.relative_to(root)),
                      image={'data': base64.b64encode(image.read_bytes()).decode(), 'mimeType': 'image/png'})
    elif report.get('kind') == 'native_files':
        metadata = report.get('inputs', {}).get('sourceMetadata', {})
        checksums = {name.removeprefix('files/'): report['frozenDigests'][name]
                     for name in report.get('nativeFiles', []) if name.startswith('files/')}
        preview = publisher_preview(metadata, Path(report['reportPath']).parent/'files', root, checksums)
        result.update(preview)
        if preview.get('image'):
            suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}[preview['image']['mimeType']]
            image = output/('publisher-preview'+suffix)
            image.write_bytes(base64.b64decode(preview['image']['data']))
            result['thumbnailRef'] = str(image.relative_to(root))
    return result


@measure('prepare')
def prepare_asset(payload, legacy, root):
    from runtime.assets import prepare, prepare_online, prepare_local_native
    if sum(key in payload for key in ('assetId', 'url', 'sourceFile', 'resourceFile')) != 1:
        raise ValueError('asset preparation requires exactly one assetId/url/sourceFile/resourceFile')
    if not payload.get('resourceFile') and 'dependencyFiles' in payload:
        raise ValueError('dependencyFiles require a native resourceFile')
    if not payload.get('resourceFile') and 'mdlSearchRoots' in payload:
        raise ValueError('mdlSearchRoots require a native resourceFile')
    if payload.get('mode') == 'articulated':
        raise ValueError('automatic articulated conversion is not implemented')
    if payload.get('sourceFile'):
        report = generate_asset(payload, legacy, root)
    elif payload.get('resourceFile'):
        report = prepare_local_native(legacy, root, payload['resourceFile'], payload.get('dependencyFiles', []),
                                      name=payload.get('name', ''), license=payload.get('license', ''),
                                      source_page=payload.get('sourcePage', ''), project_id=payload.get('projectId'),
                                      mdl_search_roots=payload.get('mdlSearchRoots'))
    elif payload.get('url'):
        report = prepare_online(legacy, root, payload['url'], source_page=payload.get('sourcePage', ''),
                                name=payload.get('name', ''), license=payload.get('license', ''),
                                project_id=payload.get('projectId'), import_ref=payload.get('importRef'))
    else:
        report = prepare(legacy, root, payload['assetId'], project_id=payload.get('projectId'))
    return publish_asset_result(report, payload, root)


class RubricUnavailable(RuntimeError):
    """The generated grader did not produce a valid, committed task result."""


def _rubric_json(value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(data.encode()) > 1_000_000:
        raise ValueError('rubric JSON exceeds the bounded message size')
    return data


def validate_rubric_output(value):
    if not isinstance(value, dict) or set(value) != {'state', 'result'}:
        raise ValueError('rubric must return state and result')
    result = value['result']
    if not isinstance(result, dict) or set(result) != {'success', 'terminated', 'reward', 'details'}:
        raise ValueError('rubric result requires success, terminated, reward and details')
    if result['success'] is not None and type(result['success']) is not bool:
        raise ValueError('rubric success must be boolean or null')
    if type(result['terminated']) is not bool:
        raise ValueError('rubric terminated must be boolean')
    if result['reward'] is not None and (type(result['reward']) not in (int, float) or not math.isfinite(result['reward'])):
        raise ValueError('rubric reward must be a finite number or null')
    _rubric_json(value)
    return value


def rubric_entry():
    """Pure facts protocol: deliberately provides no robot proxy or action RPC."""
    import contextlib
    import types
    setup = json.loads(sys.stdin.readline(1_000_001))
    os.chdir(setup['arrayRoot'])
    sources = setup['sourceFiles']
    directory = Path(tempfile.mkdtemp(prefix='frozen-rubric-')).resolve()
    for name, source in sources.items():
        path = within(directory, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    sys.path.insert(0, str(directory))
    with contextlib.redirect_stdout(sys.stderr):
        evaluate = program(sources[setup['sourceRef']], setup['sourceRef'].removesuffix('.py').replace('/', '.'),
            filename=directory/setup['sourceRef'])['evaluate']
    print('{"ready":true}', flush=True)
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if message.get('close') is True:
                break
            if message['phase'] not in ('reset', 'transition'):
                raise ValueError('unknown rubric phase')
            facts = dict(message['facts'], phase=message['phase'])
            with contextlib.redirect_stdout(sys.stderr):
                value = evaluate(types.SimpleNamespace(**deepcopy(setup['task'])), facts, message['state'])
            validate_rubric_output(value)
            response = {'value': value}
        except BaseException as error:
            response = {'error': {'type': type(error).__name__, 'message': str(error)}}
        print(_rubric_json(response), flush=True)


class RubricSession:
    """Stage grading privately; only Recorder.accept publishes a new JSON state."""
    def __init__(self, task, selected, max_seconds=30, *, record_root=None):
        if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or max_seconds <= 0:
            raise ValueError('rubric budget must be positive finite seconds')
        files = selected['sourceFiles']
        expected = {selected['sourceRef']: selected['sourceDigest'], **selected['helperDigests']}
        if set(files) != set(expected) or any(hashlib.sha256(files[name].encode()).hexdigest() != value for name, value in expected.items()):
            raise ValueError('rubric frozen source digest mismatch')
        self.descriptor = {key: deepcopy(selected[key]) for key in ('sourceRef', 'sourceDigest', 'helperDigests')}
        self.state, self.last_result, self.unavailable = None, None, None
        self._pending, self._begun = None, False
        self.remaining = float(max_seconds)
        self.record_root = Path(record_root).resolve() if record_root is not None else None
        self._array_view = tempfile.TemporaryDirectory(prefix='rubric-facts-')
        self.array_root = Path(self._array_view.name)
        self._array_cache = {}
        self.process = subprocess.Popen(controller_command('--rubric', arrays=self.array_root), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            os.set_blocking(stream.fileno(), False)
        self._buffer, self._errors = b'', b''
        try:
            ready = self._exchange({'task': asdict(task), 'sourceRef': selected['sourceRef'], 'sourceFiles': files,
                'arrayRoot': str(self.array_root)})
            if ready != {'ready': True}:
                raise ValueError('rubric process did not initialize its pure facts protocol')
        except BaseException:
            self.close()
            raise

    @measure('rubricExchange')
    def _exchange(self, message):
        import select
        started = time.monotonic()
        deadline = started+self.remaining
        try:
            pending = memoryview((_rubric_json(message)+'\n').encode())
            while pending or b'\n' not in self._buffer:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('rubric scoring budget exceeded')
                readable = [self.process.stdout, self.process.stderr]
                reads, writes, _ = select.select(readable, [self.process.stdin] if pending else [], [], min(remaining, .1))
                if writes:
                    count = os.write(self.process.stdin.fileno(), pending)
                    if not count:
                        raise BrokenPipeError('rubric input pipe closed')
                    pending = pending[count:]
                for stream in reads:
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        if stream is self.process.stdout and b'\n' not in self._buffer:
                            raise RuntimeError('rubric exited with an incomplete result: '+self._errors.decode(errors='replace'))
                        continue
                    if stream is self.process.stderr:
                        self._errors = (self._errors+chunk)[-4000:]
                    else:
                        self._buffer += chunk
                        if len(self._buffer) > 1_000_000:
                            raise ValueError('rubric output exceeds bounded message size')
            line, self._buffer = self._buffer.split(b'\n', 1)
            if self._buffer:
                raise ValueError('rubric emitted unsolicited protocol output')
            result = json.loads(line)
            if not isinstance(result, dict):
                raise ValueError('rubric protocol result must be an object')
            if 'error' in result:
                raise RuntimeError(str(result['error']))
            return result
        except Exception as error:
            self.unavailable = {'type': type(error).__name__, 'message': str(error)}
            self.close()
            raise RubricUnavailable(str(error)) from error
        finally:
            self.remaining -= time.monotonic()-started

    @measure('rubricArrays')
    def _prepare_arrays(self, facts):
        from runtime.records import array_references, read_array
        references = {}
        for reference in array_references(facts):
            name = reference['ref']
            if name in references and references[name] != reference:
                raise ValueError('conflicting array reference for one path')
            if name in self._array_cache and self._array_cache[name][0] != reference:
                raise ValueError('conflicting array reference for one path')
            references[name] = reference
        if references and self.record_root is None:
            raise ValueError('bulk grading facts require their actual record root')
        current = {}
        for name, reference in references.items():
            cached = self._array_cache.get(name)
            entry = cached if cached is not None else (deepcopy(reference), {})
            read_array(self.record_root, reference, _cache=entry[1])
            destination = within(self.array_root, name)
            relative = self.array_root/name
            if cached is None and not relative.exists() and not relative.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.record_root/name, destination)
            read_array(self.array_root, reference, _cache=entry[1])
            current[name] = entry
        for path in sorted(self.array_root.rglob('*'), key=lambda path: len(path.parts), reverse=True):
            if path.is_dir() and not path.is_symlink():
                if not any(path.iterdir()):
                    path.rmdir()
            elif path.relative_to(self.array_root).as_posix() not in references:
                path.unlink()
        self._array_cache = current

    def preview(self, phase, facts):
        if phase not in ('reset', 'transition'):
            raise ValueError('rubric phase must be reset or transition')
        if self.unavailable:
            raise RubricUnavailable(self.unavailable['message'])
        if phase == 'reset' and self._begun:
            raise ValueError('rubric reset is recorded only once')
        if phase == 'transition' and not self._begun:
            raise ValueError('rubric requires the unique recorded reset')
        if self.last_result is not None and self.last_result['terminated']:
            raise ValueError('rubric episode already terminated')
        self._pending = None
        self._prepare_arrays(facts)
        response = self._exchange({'phase': phase, 'facts': facts, 'state': deepcopy(self.state)})
        try:
            if set(response) != {'value'}:
                raise ValueError('rubric response must contain one value')
            staged = validate_rubric_output(response['value'])
        except (ValueError, TypeError, OverflowError) as error:
            self.unavailable = {'type': type(error).__name__, 'message': str(error)}
            self.close()
            raise RubricUnavailable(str(error)) from error
        self._pending = deepcopy(staged)
        return deepcopy(staged)

    def accept(self, staged):
        if self.unavailable or self._pending is None or staged != self._pending:
            raise ValueError('only the unchanged staged rubric result can be committed')
        self.state, self.last_result = deepcopy(staged['state']), deepcopy(staged['result'])
        self._begun = True
        self._pending = None

    def close(self):
        if getattr(self, 'process', None) is None:
            return
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                stream.close()
        if getattr(self, '_array_view', None) is not None:
            self._array_view.cleanup()
        self._array_cache.clear()


class Proxy:
    """Generated code receives messages and a task copy, never the host environment."""
    def __init__(self):
        self.output = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)

    def request(self, action, **fields):
        print(json.dumps({'action': action, **fields}, allow_nan=False), file=self.output, flush=True)
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError('host connection closed')
        reply = json.loads(line)
        if 'error' in reply:
            raise RuntimeError(reply['error'])
        return reply.get('result')

    def open(self):
        return self.request('open')

    def reset(self, seed=0):
        return self.request('reset', seed=seed)

    def observe(self):
        return self.request('observe')

    def step(self, action, seconds=None):
        return self.request('step', values=action, seconds=seconds)


def controller_entry():
    import contextlib
    import types
    setup = json.loads(sys.stdin.readline())
    frozen_files = setup.get('sourceFiles', {})
    source_directory = None
    if frozen_files:
        source_directory = Path(tempfile.mkdtemp(prefix='frozen-controller-')).resolve()
        for name, source in frozen_files.items():
            target = source_directory / name
            if not target.resolve().is_relative_to(source_directory):
                raise ValueError('invalid controller source path')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        sys.path.insert(0, str(source_directory))
    proxy = Proxy()
    proxy.open()
    task = types.SimpleNamespace(**setup['task'])
    if setup['kind'] in ('expert', 'policy', 'case', 'candidate'):
        proxy.reset(setup['seed'])
        entry = setup.get('sourceRef', 'expert.py')
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        with contextlib.redirect_stdout(sys.stderr):
            program(setup['source'], entry.removesuffix('.py').replace('/', '.'),
                    filename=source_directory/entry if source_directory is not None else None)['run'](proxy, task)
    elif setup['kind'] == 'isolation':
        checks = {}
        for label, path, write in setup['probes']:
            try:
                with open(path, 'w' if write else 'r') as file:
                    if write:
                        file.write('unauthorized')
                    else:
                        file.read(1)
                checks[label] = False
            except (PermissionError, FileNotFoundError, OSError):
                checks[label] = True
        import socket
        try:
            socket.create_connection(('1.1.1.1', 80), timeout=.3)
            checks['networkBlocked'] = False
        except OSError:
            checks['networkBlocked'] = True
        proxy.request('isolation_result', checks=checks)
    else:
        raise ValueError('unknown controller role')
    print(json.dumps({'action': 'close'}), file=proxy.output, flush=True)
    proxy.output.close()


class ControllerExecutionError(RuntimeError):
    def __init__(self, message, execution_status):
        super().__init__(message)
        self.execution_status = execution_status


def run_controller(env, task, source=None, kind='expert', max_seconds=120, probes=None, source_files=None, source_ref=None):
    import select
    from runtime.robot import control_entrypoints
    if kind not in ('expert', 'policy', 'case', 'candidate', 'isolation'):
        raise ValueError('unknown controller role')
    if kind != 'isolation' and getattr(env, 'record', True) is False:
        raise ValueError('formal controller execution requires committed recording')
    public_task = {name: deepcopy(getattr(task, name)) for name in ('id', 'instruction', 'bindings', 'parameters')}
    setup = {'kind': kind, 'task': public_task, 'seed': env.world.seed}
    if source is not None:
        setup['source'] = source
        setup['sourceFiles'] = source_files or {}
        setup['sourceRef'] = source_ref or 'expert.py'
    if probes:
        setup['probes'] = probes
    sensors = getattr(env, 'sensor_directory', None)
    sensors = sensors if sensors is not None and sensors.is_dir() else None
    resources = getattr(env, 'source_directory', Path('/nonexistent'))/'dependencies/resources'
    resources = resources if resources.is_dir() else None
    base_resources = getattr(env, 'source_directory', Path('/nonexistent'))/'base/dependencies/resources'
    base_resources = base_resources if base_resources.is_dir() else None
    resource_refs = {}
    for reference, name in getattr(env, 'resource_refs', {}).items():
        path = Path(name)
        for prefix, folder, public in (('dependencies/resources',resources,'/resources'),
                                       ('base/dependencies/resources',base_resources,'/base-resources')):
            if path.is_relative_to(prefix):
                relative = path.relative_to(prefix)
                if folder is None or not within(folder, relative).is_file():
                    raise ValueError('controller resource is absent from its frozen resource closure')
                resource_refs[reference] = public+'/'+relative.as_posix()
                break
        else:raise ValueError('controller resource leaves its explicit frozen resource roots')
    options = {'sensors':sensors, 'resources':resources, 'gpu':kind != 'isolation'}
    if base_resources is not None:options['base_resources'] = base_resources
    process = subprocess.Popen(controller_command(**options), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, close_fds=True)
    observations, actions, attempts, rejected, resets, opens, isolation = 0, 0, 0, 0, 0, 0, None
    environment_failure = None
    deadline = time.monotonic() + max_seconds
    pending, errors = b'', b''
    streams = [process.stdout, process.stderr]

    def send(message):
        data = memoryview((json.dumps(message, allow_nan=False)+'\n').encode())
        while data:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('controller wall-clock budget exceeded')
            _, writable, _ = select.select([], [process.stdin], [], min(1, remaining))
            if writable:
                try:
                    written = os.write(process.stdin.fileno(), data)
                except BlockingIOError:
                    continue
                if not written:
                    raise BrokenPipeError('controller input pipe closed')
                data = data[written:]

    try:
        os.set_blocking(process.stdin.fileno(), False)
        send(setup)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('controller wall-clock budget exceeded')
            if b'\n' not in pending:
                ready, _, _ = select.select(streams, [], [], min(1, max(0, deadline-time.monotonic())))
                for stream in ready:
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        streams.remove(stream)
                    elif stream is process.stderr:
                        errors = (errors+chunk)[-4000:]
                    else:
                        pending += chunk
                        if len(pending) > 1_000_000:
                            raise ValueError('controller message exceeds output limit')
                if b'\n' not in pending:
                    if process.stdout not in streams:
                        if pending:
                            raise ValueError('controller ended with an incomplete message')
                        break
                    continue
            line, pending = pending.split(b'\n', 1)
            message = json.loads(line)
            action = message.get('action')
            if action == 'close':
                break
            host_started = time.monotonic()
            try:
                if action == 'open':
                    result = {'taskId': task.id, 'observation': {} if kind == 'isolation' else env.observe(task),
                              'io': deepcopy(env.io), 'entrypoints': control_entrypoints(kind),
                              'resetSpec': {'distribution': 'deterministic_frozen_source', 'seed': env.world.seed},
                              'resources': deepcopy(resource_refs),
                              'compute': {'device': None if kind == 'isolation' else 'cuda:0',
                                  'helpers': [] if kind == 'isolation' else ['runtime.motion.create_planner', 'runtime.motion.plan_trajectory'],
                                  'scope': 'isolated assigned-GPU numerical compute; no host stage access'},
                              'sensorSpec': {'root': '/sensors' if sensors is not None else None,
                                  'arrayRoot': '/sensors' if sensors is not None else None,
                                  'cameraIds': task.measurements.get('camera', {}).get('cameraIds', [])}}
                    opens += 1
                elif action == 'reset':
                    env.reset(message.get('seed', env.world.seed))
                    result = env.observe(task)
                    resets += 1
                elif action == 'observe':
                    result = {} if kind == 'isolation' else env.observe(task, truth=False)
                    observations += 1
                elif action == 'step':
                    attempts += 1
                    if kind == 'isolation':
                        raise ValueError('filesystem isolation probes have no actuator access')
                    period = 1/env.io['clock']['controlRateHz']
                    seconds = message.get('seconds')
                    seconds = period if seconds is None else seconds
                    request_limit = max(.1, period)
                    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= request_limit:
                        raise ValueError(f'one actuator request requires finite positive seconds <= {request_limit:g} (control period {period:g})')
                    result = env.step(message['values'], seconds)
                    actions += 1
                elif action == 'isolation_result' and kind == 'isolation':
                    isolation = message['checks']
                    result = isolation
                else:
                    raise ValueError('operation is outside declared controller API')
                if isinstance(result, dict):
                    last = getattr(env, 'last_result', None)
                    truncated = bool(result.get('truncated', False) or getattr(env, 'truncated', False)
                                     or getattr(getattr(env, 'recorder', None), 'index', {}).get('truncated', False)
                                     or (result.get('motion') or {}).get('truncated', False))
                    public_result = {key: deepcopy(last[key]) for key in ('success', 'terminated', 'reward')} if last is not None else None
                    result = {**result, 'result': public_result, 'terminated': bool(last and last['terminated']),
                              'truncated': truncated,
                              'terminationPolicy': 'generated_rubric_at_committed_cycle'}
                reply = {'result': result}
            except Exception as exc:
                if action == 'step':
                    rejected += 1
                if (getattr(env, '_failed', None)
                        or getattr(getattr(env, 'recorder', None), 'failed', False)
                        or action in ('open', 'observe')
                        or action == 'reset' and not isinstance(exc, (ValueError, TypeError))):
                    environment_failure = f'{type(exc).__name__}: {exc}'
                reply = {'error': f'{type(exc).__name__}: {exc}'}
            finally:
                # Trusted physics/rendering RPC time belongs to the parent job budget.
                deadline += time.monotonic() - host_started
            send(reply)
        exit_started = time.monotonic()
        exit_deadline = deadline
        process.stdin.close()
        output_tail, pending = pending[-4000:], b''
        flush_performance('controller:waiting_exit')
        while streams or process.poll() is None:
            remaining = exit_deadline-time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select(streams, [], [], min(.05, remaining))
            for stream in ready:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    streams.remove(stream)
                elif stream is process.stderr:
                    errors = (errors+chunk)[-4000:]
                else:
                    # After close this is diagnostic output, never another environment RPC.
                    output_tail = (output_tail+chunk)[-4000:]
        code = process.poll()
        exit_diagnostics = {'exitCode': code, 'waitSeconds': time.monotonic()-exit_started,
            'remainingSecondsAtClose': max(0., exit_deadline-exit_started),
            'pipesClosed': not streams, 'stdoutTail': output_tail.decode(errors='replace'),
            'stderrTail': errors.decode(errors='replace')}
        if streams or code is None:
            flush_performance('controller:exit_timeout')
            message = environment_failure or 'isolated controller did not exit and close its pipes within its remaining controller budget'
        else:
            flush_performance('controller:exited')
            message = environment_failure or ('isolated controller failed: '+exit_diagnostics['stderrTail']) if code or environment_failure else None
        if message:
            error = ControllerExecutionError(message, 'environment_error' if environment_failure else 'policy_error')
            error.add_note('controller exit diagnostics: '+json.dumps(exit_diagnostics, ensure_ascii=False))
            raise error
        return {'kind': kind, 'isolated': True, 'openCalls': opens, 'resetCalls': resets, 'observeCalls': observations, 'actionCalls': actions, 'actionAttempts': attempts, 'rejectedActionCalls': rejected,
                'exitDiagnostics': exit_diagnostics,
                'isolationChecks': isolation, 'policyDigest': hashlib.sha256((source or (digest(Path(__file__)) + ':' + kind)).encode()).hexdigest(),
                'policyObservation': 'fixed_self_and_declared_sensors_with_committed_readonly_pixels',
                'actionEntrypoints': control_entrypoints(kind)}
    except TimeoutError as exc:
        diagnostics = {'controllerSeconds': max_seconds,
            'computeSeconds': max_seconds + time.monotonic() - deadline,
            'openCalls': opens, 'resetCalls': resets, 'observeCalls': observations,
            'actionCalls': actions, 'actionAttempts': attempts, 'rejectedActionCalls': rejected,
            'stderrTail': errors.decode(errors='replace')}
        exc.add_note('controller timeout diagnostics: '+json.dumps(diagnostics, ensure_ascii=False))
        raise
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


def frozen_sources(directory, manifest):
    files = {name: within(directory, name).read_text() for name in manifest['sourceDigests']}
    if any(hashlib.sha256(files[name].encode()).hexdigest() != value for name, value in manifest['sourceDigests'].items()):
        raise ValueError('frozen source digest mismatch')
    return files


def _same_json(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


@measure('rubricReplay')
def recompute_rubric(task, selected, directory, *, io=None, _transitions=None):
    """Rebuild only from the unique initial fact and the committed record prefix."""
    from runtime.records import read_transitions, reset_facts, cycle_facts, _physical_digest, load_batch
    transitions = read_transitions(directory) if _transitions is None else _transitions
    rows, index, tail = transitions
    descriptor = {key: selected[key] for key in ('sourceRef', 'sourceDigest', 'helperDigests')}
    if index.get('rubric') != descriptor:
        raise ValueError('recorded rubric differs from the frozen task source')
    if rows:
        load_batch(directory, io=io, _transitions=transitions)
    elif io is not None and index['io'] != io:
        raise ValueError('reset I/O differs from the frozen environment')
    receipt_path = Path(directory)/'manifest.json'
    budget = json.loads(receipt_path.read_text()).get('limits', {}).get('rubricSeconds', 30) if receipt_path.is_file() else 30
    session = RubricSession(task, selected, max_seconds=budget, record_root=directory)
    count, termination = 0, None
    unavailable = False
    try:
        facts = reset_facts(index['initialSnapshot'])
        initial = index['initialRubric']
        sequence = [(facts, initial['result'], initial.get('state'), initial['scoreStatus'], initial.get('scoreError'))]
        sequence.extend((cycle_facts(row), row['rubricResult'], row.get('rubricState'), row['scoreStatus'], row.get('scoreError')) for row in rows)
        for position, (facts, saved_result, saved_state, status, error) in enumerate(sequence):
            if termination is not None or unavailable:
                raise ValueError('facts exist after generated termination or unavailable grading')
            if status == 'unavailable':
                if not isinstance(error, dict) or not error.get('message') or saved_result != {
                        'success': None, 'terminated': False, 'reward': None, 'details': {'scoreUnavailable': True}}:
                    raise ValueError('unavailable rubric must retain an explicit ungraded diagnostic')
                unavailable = True
                continue  # Never upgrade an originally unavailable grade by retrying it.
            if status != 'available':
                raise ValueError('unknown rubric availability status')
            staged = session.preview('reset' if position == 0 else 'transition', facts)
            if not _same_json(staged['result'], saved_result) or not _same_json(staged['state'], saved_state):
                raise RubricUnavailable('frozen rubric does not reproduce the committed result/state')
            session.accept(staged)
            if position:
                count += 1
            if saved_result['terminated']:
                termination = position-1
        result = deepcopy(index['lastResult'])
        return {'result': result, 'scoreStatus': 'unavailable' if unavailable else 'available',
                'rubric': descriptor, 'updateCount': count, 'terminationCycle': termination,
                'factsDigest': _physical_digest({'initialSnapshot': index['initialSnapshotDigest'],
                                               'cycles': [row['factsDigest'] for row in rows]}),
                'committedBytes': index['committedBytes'], 'uncommittedRecordTail': tail}
    finally:
        session.close()


def robot_setup_sources(setup):
    """Freeze only data profiles and explicit installation declarations."""
    from runtime.robot import parse_profile, native_values
    if (not isinstance(setup, dict) or not {'robots','resourceRefs','sourceFiles','sourceDigests'}.issubset(setup)
            or set(setup)-{'robots','resourceRefs','sourceFiles','sourceDigests','recording'}):
        raise ValueError('robotSetup requires robot declarations, resource references and exact profile sources')
    robots, names, ids = [], set(), set()
    if not isinstance(setup['robots'], list) or not setup['robots']:
        raise ValueError('robotSetup requires at least one robot')
    for original in setup['robots']:
        if (not isinstance(original, dict) or not {'id','profileRef'}.issubset(original)
                or set(original)-{'id','profileRef','owner','frame','at','orientation','purpose'}):
            raise ValueError('invalid robot installation declaration')
        robot = {'at':[0,0,0], 'orientation':[1,0,0,0], **deepcopy(original)}
        id = valid_id(robot['id'])
        if id in ids:raise ValueError('duplicate robot instance ID')
        ids.add(id)
        name = robot['profileRef']
        if (not isinstance(name, str) or not name.endswith('.robot.json') or Path(name).is_absolute()
                or '..' in Path(name).parts or '\\' in name or Path(name).as_posix() != name):
            raise ValueError('robot profileRef requires a plain project-relative *.robot.json')
        names.add(name)
        for field in ('owner','frame'):
            if field in robot and robot[field] is not None:valid_id(robot[field])
        native_values(robot['at'], [3], 'installation position')
        q = native_values(robot['orientation'], [4], 'installation orientation')
        if abs(float(q@q)-1) > 1e-8:raise ValueError('installation orientation must be a unit wxyz quaternion')
        if 'purpose' in robot and not isinstance(robot['purpose'], str):raise ValueError('robot purpose must be text')
        robots.append(robot)
    files, hashes = setup['sourceFiles'], setup['sourceDigests']
    if not isinstance(files, dict) or not isinstance(hashes, dict) or set(files) != names or set(hashes) != names:
        raise ValueError('robot profiles require their exact source closure')
    for name, source in files.items():
        if not isinstance(source, str) or hashlib.sha256(source.encode()).hexdigest() != hashes[name]:
            raise ValueError('robot profile source digest differs from its snapshot')
        parse_profile(source)
    refs = setup['resourceRefs']
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs) or len(set(refs)) != len(refs):
        raise ValueError('robot resourceRefs must be unique prepared references')
    result = {'robots':robots, 'resourceRefs':deepcopy(refs), 'sourceFiles':deepcopy(files), 'sourceDigests':deepcopy(hashes)}
    if 'recording' in setup:
        if not isinstance(setup['recording'], dict):raise ValueError('robot recording must map frozen task IDs to declarations')
        result['recording'] = deepcopy(setup['recording'])
    return result


def robot_setup_tasks(tasks, recording):
    """Only append observation views; task semantics and policy permissions stay frozen."""
    from runtime.scene import validate_recording
    if not isinstance(recording, dict) or set(recording)-{row['id'] for row in tasks}:
        raise ValueError('robot recording references an unknown frozen task')
    result = deepcopy(tasks)
    for row in result:
        if row['id'] not in recording:continue
        original, replacement = Task(**row).recording, recording[row['id']]
        validate_recording(replacement)
        if (replacement['cameras'][:len(original['cameras'])] != original['cameras']
                or original['primaryCameraId'] is not None and replacement['primaryCameraId'] != original['primaryCameraId']):
            raise ValueError('robot recording must preserve original cameras and primary camera')
        row['recording'] = deepcopy(replacement)
    return result


def _robot_setup_entry(request, root):
    """Fixed configuration authoring inside the existing native-build sandbox."""
    from pxr import Usd, UsdGeom
    from runtime.simulation import compile_recording_cameras
    world = World.restore(request['baseWorld'])
    for node in world.nodes.values():
        for field in ('path','material'):
            if node.get(field):node[field] = 'base/'+node[field]
    world.stage = Usd.Stage.CreateNew(str(root/'scene-source.usda'))
    world.stage.GetRootLayer().subLayerPaths = ['base/'+request['baseUsd']]
    world.stage.SetDefaultPrim(world.stage.GetPrimAtPath('/World'))
    UsdGeom.SetStageMetersPerUnit(world.stage, 1)
    UsdGeom.SetStageUpAxis(world.stage, UsdGeom.Tokens.z)
    world.resources = deepcopy(request['resources'])
    world.source_root = Path('/inputs')
    installed = []
    for declaration in request['robotSetup']['robots']:
        declaration = dict(declaration)
        id, profile = declaration.pop('id'), declaration.pop('profileRef')
        if declaration.get('frame') == 'world':declaration.pop('frame')
        installed.append(world.robot(id, profile, **declaration))
    tasks = robot_setup_tasks(request['tasks'], request['robotSetup'].get('recording', {}))
    cameras = deepcopy(request['recordingCameras'])
    for row in tasks:
        task = Task(**row);task.bind(world)
        if task.id in request['robotSetup'].get('recording', {}):
            cameras[task.id] = compile_recording_cameras(world.stage, world, task)
    world.stage.GetRootLayer().Save()
    save(root/'scene.json', world.snapshot())
    save(root/'tasks.json', tasks)
    save(root/'native-build.json', {'recordingCameras':cameras, 'robots':installed})


def robot_environment(root, project, directory, manifest, world, payload, *, write=False):
    """Resolve one base plus its immutable robot installation; no global latest setup."""
    setup, reference = payload.get('robotSetup'), payload.get('robotSetupRef')
    if manifest.get('_robotSetup'):
        _, directory, manifest, world = frozen(root, {'projectId':manifest['projectId'], 'revision':manifest['revision']})
    if setup is None and reference is None:return directory, manifest, world, set()
    if setup is not None and reference is not None:raise ValueError('robotSetup and robotSetupRef are mutually exclusive')
    if manifest.get('capabilities', {}).get('robotSetup') is not True:
        raise ValueError('this frozen environment does not support robot installation; preserve it and build a compatible new base')
    base_path = within(root, manifest['artifacts']['manifest'])
    base_hash = digest(base_path)
    expected = None
    if setup is not None:
        setup = robot_setup_sources(setup)
        reports = {}
        from runtime.assets import _cache_valid
        for ref in setup['resourceRefs']:
            path = within(root, ref)
            if not path.is_relative_to(root/'assets') or path.name != 'report.json' or not _cache_valid(path.parent):
                raise ValueError('robot resource is not a verified prepared asset closure')
            report = json.loads(path.read_text())
            if report.get('kind') != 'native_files':raise ValueError('robot resource requires native_files')
            reports[ref] = digest(path)
        expected = {'baseManifestRef':manifest['artifacts']['manifest'], 'baseManifestSha256':base_hash,
            'baseRevision':manifest['revision'], 'runtime':manifest['runtime'], 'setup':setup, 'resourceReports':reports,
            'installationJobId':valid_id(payload.get('jobId'))}
        identity = revision_key(**expected)
        path = project/'robot-setups'/identity/'manifest.json'
        reference = str(path.relative_to(root))
    else:
        path = within(root, reference)
        if path.name != 'manifest.json' or path.parent.parent != project/'robot-setups':
            raise ValueError('robot installation reference leaves this project')
    if not path.exists():
        if not write or expected is None:raise ValueError('robot installation snapshot does not exist')
        from runtime.simulation import compile_native_world
        from runtime.robot import resolve_resource
        path.parent.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='installing-', dir=path.parent.parent))
        base_names = set(manifest['assets']) | set(manifest.get('frozenArtifactDigests', {})) | set(manifest['sourceDigests'])
        try:
            for name in sorted(base_names):
                source = within(directory, name);target = within(temporary/'base', name)
                target.parent.mkdir(parents=True, exist_ok=True);shutil.copy2(source, target)
            for name in manifest['sourceDigests']:
                target = within(temporary, name);target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(within(directory, name), target)
            resources = {ref:'base/'+entry for ref, entry in manifest.get('resources', {}).items()}
            added = freeze_resources(root, setup['resourceRefs'], temporary)
            for ref, entry in added.items():
                if ref in resources:
                    _, bundle = resolve_resource({'resourceRef':ref}, {ref:str(directory/manifest['resources'][ref])})
                    if digest(bundle/'report.json') != reports[ref]:raise ValueError('robot resource would replace a different frozen resource')
                else:resources[ref] = entry
            inputs = temporary/'install-inputs';inputs.mkdir()
            for name, source in setup['sourceFiles'].items():
                target = within(inputs, name);target.parent.mkdir(parents=True, exist_ok=True);target.write_text(source)
            request = {'seed':manifest['seed'], 'robotSetup':setup, 'resources':resources,
                'baseWorld':json.loads((directory/'scene.json').read_text()),
                'baseUsd':manifest['compiled']['usd'], 'tasks':manifest['tasks'],
                'recordingCameras':manifest['compiled'].get('recordingCameras', {})}
            progress('robot_installation', '正在按冻结配置安装机器人', revision=manifest['revision'])
            with (temporary/'installation.log').open('wb') as log:
                completed = subprocess.run(native_build_command(inputs, temporary, root, Path(payload.get('legacyRoot','')).resolve()),
                    input=json.dumps(request).encode(), stdout=log, stderr=subprocess.STDOUT, close_fds=True)
            failure = temporary/'installation-failure.json'
            if failure.is_file():
                raise RuntimeError('isolated robot installation failed: '+json.loads(failure.read_text())['error']+'; see installation.log')
            if completed.returncode:raise RuntimeError('isolated robot installation failed; see installation.log')
            for name in base_names:
                if digest(within(temporary/'base', name)) != digest(within(directory, name)):
                    raise ValueError('robot installation changed its base closure')
            for name, source in setup['sourceFiles'].items():
                if within(inputs, name).read_text() != source:raise ValueError('robot installation changed its profile source')
            # Keep profile bytes in the immutable input lock, outside the grading sources.
            inputs.rename(temporary/'profiles')
            audit = isolated_native_audit(temporary, timeout=worker_timeout(payload));save(temporary/'native-stage-audit.json', audit)
            installed_world = World.restore(json.loads((temporary/'scene.json').read_text()))
            authored = json.loads((temporary/'native-build.json').read_text())
            compiler = isolated_native_compile if audit.get('mdl') else compile_native_world
            compiled = compiler(installed_world, temporary, installed_world.io,
                recording_cameras=authored['recordingCameras'], camera=manifest.get('camera'))
            compiled['nativeStageAudit'] = 'native-stage-audit.json'
            frozen_names = {'scene-source.usda','scene.json','tasks.json','native-stage-audit.json'}
            source_names = set(manifest['sourceDigests'])
            hashes = {str(p.relative_to(temporary)):digest(p) for p in temporary.rglob('*')
                      if p.is_file() and str(p.relative_to(temporary)) not in set(NATIVE_DIAGNOSTICS.values())}
            environment = {'compiled':compiled, 'io':compiled['io'], 'resources':resources,
                'tasks':json.loads((temporary/'tasks.json').read_text()),
                'assets':{name:value for name,value in hashes.items() if name not in frozen_names|source_names},
                'frozenArtifactDigests':{name:hashes[name] for name in frozen_names}, 'sourceDigests':deepcopy(manifest['sourceDigests'])}
            descriptor = {'schema':'sceneagent-robot-setup-v1', 'projectId':manifest['projectId'],
                'inputs':expected, 'environment':environment}
            descriptor['diagnostics'] = {name:digest(temporary/name) for name in NATIVE_DIAGNOSTICS.values()
                                         if (temporary/name).is_file()}
            save(temporary/'manifest.json', descriptor)
            temporary.rename(path.parent)
        except BaseException:
            # Failed installs stay under their unique diagnostic directory, never the reusable identity.
            raise
    descriptor = json.loads(path.read_text())
    inputs = descriptor.get('inputs', {})
    if (descriptor.get('schema') != 'sceneagent-robot-setup-v1' or descriptor.get('projectId') != manifest['projectId']
            or inputs.get('baseManifestRef') != manifest['artifacts']['manifest'] or inputs.get('baseManifestSha256') != base_hash
            or inputs.get('baseRevision') != manifest['revision'] or inputs.get('runtime') != manifest['runtime']
            or revision_key(**inputs) != path.parent.name or expected is not None and inputs != expected):
        raise ValueError('robot installation differs from its frozen base, configuration or identity')
    if payload.get('robotSetupSha256') is not None and payload['robotSetupSha256'] != digest(path):
        raise ValueError('robot installation manifest digest changed')
    creator = valid_id(inputs.get('installationJobId'))
    if expected is not None and creator == payload.get('jobId'):
        # Only the actual creating request may read its not-yet-published input.
        job = root/'jobs'/creator
        status = json.loads((job/'status.json').read_text())
        if status.get('state') != 'completed':
            envelope = json.loads((job/'request.json').read_text())['payload']
            request = {**envelope, **envelope.get('task', {})}
            if (status.get('jobId') != creator or status.get('state') != 'running'
                    or request.get('jobId') != creator or request.get('projectId') != manifest['projectId']
                    or request.get('revision') != manifest['revision'] or request.get('operation') != 'rollout'
                    or robot_setup_sources(request.get('robotSetup')) != expected['setup']):
                raise ValueError('unpublished robot installation does not belong to the actual creating job')
        else:
            expected = None
    if expected is None:
        validate_publication({'projectId':manifest['projectId'], 'revision':manifest['revision'],
            'publicationJobId':creator, 'artifacts':{'manifest':reference}}, root)
        published = json.loads((root/'jobs'/creator/'result.json').read_text())
        if published.get('robotSetupRef') != reference or published.get('robotSetupSha256') != digest(path):
            raise ValueError('published installation does not match its creating job result')
    setup = robot_setup_sources(inputs['setup'])
    for name, source in setup['sourceFiles'].items():
        if within(path.parent/'profiles', name).read_text() != source:raise ValueError('frozen robot profile bytes changed')
    overlay = descriptor['environment']
    if (set(overlay) != {'compiled','io','resources','tasks','assets','frozenArtifactDigests','sourceDigests'}
            or overlay['sourceDigests'] != manifest['sourceDigests']
            or overlay['io']['clock'] != manifest['io']['clock']
            or any(overlay['io'][kind].get(name) != spec for kind in ('ports','streams')
                   for name, spec in manifest['io'][kind].items())):
        raise ValueError('robot installation changed a protected environment source or interface')
    effective = {**deepcopy(manifest), **deepcopy(overlay)}
    if effective['tasks'] != robot_setup_tasks(manifest['tasks'], setup.get('recording', {})):
        raise ValueError('robot installation changed frozen task semantics or recording')
    required = {path, base_path, *verify_inputs(effective, runtime_key(), path.parent)}
    for name, checksum in descriptor.get('diagnostics', {}).items():
        if name not in NATIVE_DIAGNOSTICS.values() or digest(within(path.parent, name)) != checksum:
            raise ValueError('robot installation diagnostic log changed')
        required.add(within(path.parent, name))
    for name in set(manifest['assets']) | set(manifest.get('frozenArtifactDigests', {})) | set(manifest['sourceDigests']):
        if digest(within(path.parent/'base', name)) != digest(within(directory, name)):
            raise ValueError('robot installation base copy differs from the frozen original')
        required.add(within(path.parent/'base', name))
    effective['_robotSetup'] = {'robotSetupRef':reference, 'robotSetupSha256':digest(path)}
    installed_world = World.restore(json.loads((path.parent/'scene.json').read_text()))
    original = json.loads((directory/'scene.json').read_text())
    combined = installed_world.snapshot()
    for node in original['nodes']:
        for field in ('path','material'):
            if node.get(field):node[field] = 'base/'+node[field]
        if installed_world.nodes.get(node['id']) != node:
            raise ValueError('robot installation changed an original scene node')
    added = {row['id'] for row in setup['robots']}
    if (set(installed_world.nodes) != {node['id'] for node in original['nodes']} | added
            or any(combined.get(key) != original.get(key) for key in original if key not in ('nodes','io'))):
        raise ValueError('robot installation changed original scene semantics')
    return path.parent, effective, installed_world, required


def behavior_sources(behavior):
    if not isinstance(behavior, dict) or set(behavior) != {'sourceRef', 'helperRefs', 'sourceFiles', 'sourceDigests'}:
        raise ValueError('behavior requires an independent source snapshot and complete digest closure')
    selected = source_closure({key: behavior[key] for key in ('sourceRef', 'helperRefs')},
                              behavior['sourceFiles'], behavior['sourceDigests'])
    if set(behavior['sourceFiles']) != set(selected['sourceFiles']) or set(behavior['sourceDigests']) != set(selected['sourceFiles']):
        raise ValueError('behavior snapshot must contain exactly its declared closure')
    return selected


def behavior_snapshot(root, project, selected, *, write=False):
    descriptor = {key: selected[key] for key in ('sourceRef', 'sourceDigest', 'helperDigests')}
    key = revision_key(**descriptor)
    directory = within(project/'behaviors', key)
    path = directory/'manifest.json'
    expected = {'schema':'sceneagent-behavior-v1', **descriptor}
    if write and not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        for name, source in selected['sourceFiles'].items():
            target = within(directory, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        save(path, expected)
    if json.loads(path.read_text()) != expected:
        raise ValueError('behavior descriptor differs from its immutable source lock')
    for name, source in selected['sourceFiles'].items():
        if within(directory, name).read_text() != source:
            raise ValueError('behavior source differs from its immutable source lock')
    return str(path.relative_to(root)), {path, *(within(directory, name) for name in selected['sourceFiles'])}


def recorded_behavior(root, project, episode):
    path = within(root, episode['behaviorRef'])
    if path.parent.parent != project/'behaviors' or path.name != 'manifest.json':
        raise ValueError('behavior reference leaves the project source scope')
    descriptor = json.loads(path.read_text())
    names = [descriptor['sourceRef'], *descriptor['helperDigests']]
    files = {name: within(path.parent, name).read_text() for name in names}
    selected = behavior_sources({'sourceRef':descriptor['sourceRef'], 'helperRefs':list(descriptor['helperDigests']),
        'sourceFiles':files, 'sourceDigests':{descriptor['sourceRef']:descriptor['sourceDigest'], **descriptor['helperDigests']}})
    reference, required = behavior_snapshot(root, project, selected)
    if reference != episode['behaviorRef']:
        raise ValueError('behavior reference differs from its content identity')
    return selected, required


def _select_controller(task, files, manifest, payload):
    controller = payload.get('controller', 'expert')
    kind = controller.get('kind', controller.get('type', 'expert')) if isinstance(controller, dict) else controller
    if 'behavior' in payload:
        if kind != 'expert' or not manifest.get('capabilities', {}).get('independentBehavior'):
            raise ValueError('this frozen environment does not support the selected independent behavior')
        selected = behavior_sources(payload['behavior'])
        forbidden = {'scene.py', 'tasks.py', task.rubric['sourceRef']} | {case['sourceRef'] for case in task.evaluation['cases']}
        if forbidden.intersection(selected['sourceFiles']):
            raise ValueError('expert behavior cannot import private environment grading/case entries')
        return 'expert', selected, None
    if kind == 'expert':
        return 'expert', expert_sources(task, files, manifest['sourceDigests']), None
    if kind != 'policy' or not isinstance(controller, dict):
        raise ValueError('rollout must select expert or an explicit frozen validation case source')
    matches = [case for case in task.evaluation['cases'] if case['sourceRef'] == controller.get('policyRef')
               and (not controller.get('caseId') or controller['caseId'] == case['id'])]
    if len(matches) != 1:
        raise ValueError('policyRef must select exactly one frozen evaluation case')
    case = matches[0]
    selected = evaluation_sources(task, files, manifest['sourceDigests'])[case['id']]
    if controller.get('digest') != selected['sourceDigest']:
        raise ValueError('policy digest differs from the frozen case source')
    if payload.get('purpose') != 'verification':
        raise ValueError('generated validation cases require purpose=verification')
    return 'case', selected, case


def episode_budgets(payload, task=None):
    physical = task.execution['physicalSeconds'] if task is not None else 30
    if type(physical) not in (int, float) or not math.isfinite(physical) or not 0 < physical:
        raise ValueError('frozen physical episode duration must be finite and positive')
    result = {'physicalSeconds': physical}
    for field, limit in (('maxSeconds', 120), ('rubricSeconds', 30)):
        value = payload.get(field, limit)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value:
            raise ValueError(field+' must be positive finite seconds')
        result['controllerSeconds' if field == 'maxSeconds' else 'rubricSeconds'] = value
    return result


def _episode(root, project, directory, manifest, world, task, selected, kind, payload, *, output_parent=None, case=None, interface_probes=None):
    from runtime.records import read_transitions, execution_counts
    limits = episode_budgets(payload, task)
    files = frozen_sources(directory, manifest)
    grade_source = rubric_sources(task, files, manifest['sourceDigests'])
    episode_id = uuid.uuid4().hex[:16]
    output = (output_parent or project/'episodes')/episode_id
    output.mkdir(parents=True)
    purpose = payload.get('purpose', 'demonstration')
    receipt = {'schema': 'sceneagent-episode-v6', 'engine': 'isaacsim', 'physics': 'physx',
        'projectId': payload['projectId'], 'revision': manifest['revision'], 'taskId': task.id,
        'episodeId': episode_id, 'seed': manifest['seed'], 'jobId': payload.get('jobId'),
        'generationId': payload.get('generationId'), 'attemptId': payload.get('requestId', episode_id),
        'manifestRef': manifest['artifacts']['manifest'], 'io': deepcopy(manifest['io']),
        'recording': deepcopy(task.recording), 'limits': limits,
        'executionKind': 'actuator_physics_native' if has_control_ports(manifest['io']) else 'native_physics', 'purpose': purpose,
        'workerTimeoutSeconds': worker_timeout(payload),
        'controllerKind': kind, 'controllerSource': selected['sourceRef'], 'controllerDigest': selected['sourceDigest'],
        'controllerHelperDigests': selected['helperDigests'], 'rubric': {key: grade_source[key] for key in ('sourceRef', 'sourceDigest', 'helperDigests')},
        'status': 'running', 'demonstrationEligible': False}
    if manifest.get('_robotSetup'):
        receipt.update(manifest['_robotSetup'])
    if kind == 'expert' and 'behavior' in payload:
        receipt['behaviorRef'], _ = behavior_snapshot(root, project, selected, write=True)
    if case is not None:
        receipt.update(caseId=case['id'], expected=deepcopy(case['expected']), requirementRefs=case['requirementRefs'])
    artifacts = artifact_paths(root, output, {'annotations': 'annotations.json', 'trajectory': 'trajectory.json',
        'transitions': 'transitions.jsonl', 'records': 'records.json', 'manifest': 'manifest.json', 'report': 'report.json'})
    receipt['artifacts'] = artifacts
    save(output/'manifest.json', receipt)
    session, env, control, error, close_error = None, None, None, None, None
    outcome = 'completed'
    try:
        session = RubricSession(task, grade_source, max_seconds=limits['rubricSeconds'], record_root=output)
        env = load_environment(directory, manifest, world, output, task, episode_id=episode_id, rubric_session=session)
        receipt.update(initialState=deepcopy(env.recorder.index['initialSnapshot']['truth']))
        receipt['nativeInitialTargets'] = deepcopy(env.active_targets)
        if interface_probes is not None:
            receipt['interfaceIsolationChecks'] = run_controller(env, task, kind='isolation', probes=interface_probes)['isolationChecks']
        control = run_controller(env, task, selected['sourceFiles'][selected['sourceRef']], kind,
            max_seconds=limits['controllerSeconds'], source_files=selected['sourceFiles'], source_ref=selected['sourceRef'])
    except Exception as exc:
        error = {'type': type(exc).__name__, 'message': str(exc)}
        if getattr(exc, '__notes__', None):
            error['notes'] = list(exc.__notes__)
        fault = getattr(env, '_failed', None)
        outcome = 'score_unavailable' if isinstance(exc, RubricUnavailable) else getattr(exc, 'execution_status', 'environment_error' if env is None or fault else 'policy_error')
    finally:
        if env is not None:
            try:
                env.close(outcome)
            except Exception as exc:
                close_error = type(exc).__name__+': '+str(exc)
                if getattr(exc, '__notes__', None):
                    close_error += '\n'+'\n'.join(exc.__notes__)
                error = error or {'type': type(exc).__name__, 'message': str(exc)}
                outcome = 'environment_error'
        if session is not None:
            session.close()
    for camera in task.recording['cameras']:
        camera_id = camera['cameraId']
        video = output/'videos'/(camera_id+'.mp4')
        if video.is_file() and env is not None and any(frame['cameraId'] == camera_id for frame in env.frames):
            artifacts['video:'+camera_id] = str(video.relative_to(root))
    primary = task.recording['primaryCameraId']
    if primary is not None and 'video:'+primary in artifacts:
        artifacts['video'] = artifacts['video:'+primary]
    receipt['cameraFailures'] = deepcopy(getattr(env, 'camera_failures', {})) if env is not None else {}
    if env is None or not (output/'records.json').exists():
        for label, filename in (('nativeStepFailure', 'native-step-failure.json'), ('cameraFailure', 'camera-failure.json')):
            if (output/filename).is_file():
                artifacts[label] = str((output/filename).relative_to(root))
        report = {'taskId': task.id, 'success': None, 'terminated': False, 'reward': None, 'details': {},
                  'result': None, 'scoreStatus': 'unavailable', 'executionStatus': outcome,
                  'error': error, 'closeError': close_error, 'initializationFailed': True, 'rubric': receipt['rubric'],
                  'workerTimeoutSeconds': receipt['workerTimeoutSeconds']}
        save(output/'report.json', report)
        receipt.update(status='failed', initializationFailed=True, executionStatus=outcome, error=error,
            artifactDigests={label: digest(within(root, reference)) for label, reference in artifacts.items() if within(root, reference).is_file() and label != 'manifest'})
        save(output/'manifest.json', receipt)
        return {'projectId': payload['projectId'], 'revision': manifest['revision'], 'episode': episode_id,
                'taskId': task.id, 'score': report, 'artifacts': artifacts, 'demonstrationEligible': False,
                'executionKind': receipt['executionKind'], 'workerTimeoutSeconds': receipt['workerTimeoutSeconds'],
                **({'behaviorRef': receipt['behaviorRef']} if 'behaviorRef' in receipt else {}),
                **({**manifest['_robotSetup'], 'recording':deepcopy(task.recording)} if manifest.get('_robotSetup') else {})}
    with measure('verifyRecords'):
        transitions = read_transitions(output)
    rows, index, tail = transitions
    failure = getattr(env, '_failed', None)
    unknown = bool(failure and failure.get('countStatus') != 'known')
    if unknown:
        counts = {'controlCycles': index['committedCycles'], 'physicalSteps': index['physicalSteps'],
            'committedPhysicalSteps': index['physicalSteps'], 'totalExecutedPhysicalSteps': None,
            'uncommittedPhysicalSteps': None, 'uncommittedRecordTail': tail, 'executionCountKnown': False,
            'confirmedStepLowerBound': failure.get('confirmedStepLowerBound')}
        outcome = 'environment_error'
    else:
        actual_steps = failure['totalExecutedPhysicalSteps'] if failure else env.steps
        counts = execution_counts(output, actual_steps, _transitions=transitions)
        counts['executionCountKnown'] = True
    unavailable = index['initialRubric']['scoreStatus'] == 'unavailable' or any(row['scoreStatus'] == 'unavailable' for row in rows)
    if unavailable:
        outcome = 'score_unavailable'
    elif counts['uncommittedPhysicalSteps'] or counts['uncommittedRecordTail']:
        outcome = 'environment_error'
    elif outcome == 'completed' and index.get('truncated'):
        outcome = 'truncated'
    if failure or getattr(getattr(env, 'recorder', None), 'failed', False):
        outcome = 'environment_error'
    from runtime.records import project_annotations
    task_metadata = {key: deepcopy(asdict(task)[key]) for key in ('id', 'instruction', 'bindings', 'measurements')}
    try:
        annotations = project_annotations(rows, task_metadata, receipt['rubric'])
    except (ValueError, TypeError) as exc:
        annotations = []
        outcome = 'environment_error'
        error = error or {'type': type(exc).__name__, 'message': 'annotation export failed: '+str(exc)}
    save(output/'annotations.json', {'schema': 'sceneagent-generated-annotations-v1', 'taskId': task.id,
        'rubric': receipt['rubric'], 'bindings': task.bindings, 'annotations': annotations})
    receipt['media'] = media_summary(receipt, env.frames)
    committed = deepcopy(index['lastResult'])
    final_available = outcome not in ('environment_error', 'score_unavailable')
    result = deepcopy(committed) if final_available else None
    aliases = result if result is not None else {'success': None, 'terminated': index['terminated'],
        'reward': None, 'details': {'finalScoreUnavailable': True}}
    report = {'taskId': task.id, **aliases, 'result': result, 'lastCommittedResult': committed,
        'scoreStatus': 'available' if final_available else 'unavailable',
        'committedScoreStatus': 'unavailable' if unavailable else 'available',
        'resultScope': 'actual_committed_execution' if final_available else 'diagnostic_prefix_only',
        'scoreError': next((row['scoreError'] for row in reversed(rows) if row['scoreStatus'] == 'unavailable'), index['initialRubric'].get('scoreError')),
        'executionStatus': outcome, 'error': error, 'closeError': close_error, 'episode': episode_id, 'revision': manifest['revision'],
        'rubric': receipt['rubric'], 'control': control, 'seconds': env.time,
        'workerTimeoutSeconds': receipt['workerTimeoutSeconds'], **counts}
    eligible = bool(purpose == 'demonstration' and kind == 'expert' and outcome == 'completed'
                    and result is not None and result['success'] is True and control and control['isolated']
                    and has_control_ports(manifest['io']))
    receipt.update(status='candidate', control=control, executionStatus=outcome,
        terminated=index['terminated'], truncated=index['truncated'], endReason=outcome,
        candidateDemonstrationEligible=eligible, cameraFrames=len(env.frames), result=result, lastCommittedResult=committed,
        **counts)
    save(output/'trajectory.json', {'schema': 'sceneagent-studio-trajectory-v6', 'engine': 'isaacsim', 'physics': 'physx',
        'revision': manifest['revision'], 'io': deepcopy(manifest['io']), 'task': asdict(task),
        **deepcopy(manifest.get('_robotSetup', {})),
        'initialState': deepcopy(index['initialSnapshot']['truth']), 'frames': env.frames,
        'events': [event for row in rows for event in row['events']],
        'recording': task.recording, 'media': receipt['media'], 'cameraFailures': receipt['cameraFailures'],
        'controlRecords': artifacts['transitions']})
    save(output/'report.json', report)
    for label, filename in (('nativeStepFailure', 'native-step-failure.json'), ('cameraFailure', 'camera-failure.json')):
        if (output/filename).is_file():
            artifacts[label] = str((output/filename).relative_to(root))
    receipt['artifactDigests'] = {name: digest(within(root, reference)) for name, reference in artifacts.items()
        if name != 'manifest' and within(root, reference).is_file()}
    for label in ('nativeStepFailure', 'cameraFailure'):
        if label in artifacts:
            receipt['artifactDigests'][label] = digest(within(root, artifacts[label]))
    save(output/'manifest.json', receipt)
    return {'projectId': payload['projectId'], 'revision': manifest['revision'], 'episode': episode_id,
            'taskId': task.id, 'score': report, 'artifacts': artifacts, 'demonstrationEligible': eligible,
            'executionKind': receipt['executionKind'], 'media': receipt['media'],
            'workerTimeoutSeconds': receipt['workerTimeoutSeconds'],
            **({'behaviorRef': receipt['behaviorRef']} if 'behaviorRef' in receipt else {}),
                **({**manifest['_robotSetup'], 'recording':deepcopy(task.recording)} if manifest.get('_robotSetup') else {})}


@measure('rollout')
def rollout(payload, root):
    if 'robotSetup' in payload and 'behavior' not in payload:
        raise ValueError('new robot installation requires an independent behavior source')
    episode_budgets(payload)
    project, directory, manifest, world = frozen(root, payload)
    directory, manifest, world, _ = robot_environment(root, project, directory, manifest, world, payload, write=True)
    ids = payload.get('taskIds') or [payload.get('taskId') or manifest['tasks'][0]['id']]
    seeds = payload.get('seeds') or [manifest['seed']]
    if len(set(ids)) != len(ids) or any(type(seed) is not int or seed != manifest['seed'] for seed in seeds):
        raise ValueError('rollout requires unique tasks and the frozen deterministic seed')
    repetitions = payload.get('repetitions', 1)
    if type(repetitions) is not int or repetitions < 1 or 'seeds' in payload and 'repetitions' in payload:
        raise ValueError('repetitions requires a positive integer and cannot be combined with seeds')
    files = frozen_sources(directory, manifest)
    results = []
    for id in ids:
        task = next((Task(**row) for row in manifest['tasks'] if row['id'] == id), None)
        if task is None:
            raise ValueError('unknown task ID')
        kind, selected, case = _select_controller(task, files, manifest, payload)
        for _ in seeds * repetitions:
            results.append(_episode(root, project, directory, manifest, world, task, selected, kind, payload, case=case))
    return {**results[0], 'episodes': results, 'success': all(row['score']['success'] is True for row in results)}


def evaluate_internal(payload, root):
    """Evaluate an independent frozen candidate without modifying the environment package."""
    if 'robotSetup' in payload:
        raise ValueError('external evaluation only reuses a published robot installation reference')
    episode_budgets(payload)
    project, directory, manifest, world = frozen(root, payload)
    directory, manifest, world, _ = robot_environment(root, project, directory, manifest, world, payload)
    id = payload.get('taskId')
    task = next((Task(**row) for row in manifest['tasks'] if row['id'] == id), None)
    if task is None:
        raise ValueError('evaluate requires an explicit frozen taskId')
    if type(payload.get('seed', manifest['seed'])) is not int or payload.get('seed', manifest['seed']) != manifest['seed']:
        raise ValueError('candidate seed differs from the frozen reset')
    candidate = payload.get('candidate')
    if not isinstance(candidate, dict) or set(candidate) != {'sourceRef', 'helperRefs', 'sourceFiles', 'sourceDigests'}:
        raise ValueError('candidate requires an independent source snapshot and complete digest closure')
    selected = source_closure({key: candidate[key] for key in ('sourceRef', 'helperRefs')},
                              candidate['sourceFiles'], candidate['sourceDigests'])
    if set(candidate['sourceFiles']) != set(selected['sourceFiles']) or set(candidate['sourceDigests']) != set(selected['sourceFiles']):
        raise ValueError('candidate snapshot must contain exactly its declared closure')
    evaluation_id = valid_id(payload.get('requestId') or uuid.uuid4().hex[:16])
    output = project/'evaluations'/evaluation_id
    output.mkdir(parents=True, exist_ok=False)
    for name, source in selected['sourceFiles'].items():
        path = within(output/'candidate', name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    episode = _episode(root, project, directory, manifest, world, task, selected, 'candidate',
                       {**payload, 'purpose': 'evaluation'}, output_parent=output/'episodes')
    result = {'projectId': payload['projectId'], 'revision': manifest['revision'], 'taskId': task.id,
              'workerTimeoutSeconds': worker_timeout(payload),
              **deepcopy(manifest.get('_robotSetup', {})),
              'evaluationId': evaluation_id, 'candidate': {key: selected[key] for key in ('sourceRef', 'sourceDigest', 'helperDigests')},
              'episodes': [episode], 'result': episode['score']['result'],
              'executionStatus': episode['score']['executionStatus'], 'scoreStatus': episode['score']['scoreStatus'],
              'demonstrationEligible': False, 'artifactRef': str((output/'report.json').relative_to(root))}
    save(output/'report.json', result)
    return result


def inspect_project(payload, root):
    from runtime.robot import robot_capabilities
    current_runtime = runtime_key()
    capabilities = robot_capabilities(None, current_runtime)
    project = root / 'projects' / valid_id(payload['projectId'])
    revision = payload.get('revision')
    if not revision:
        latest = project / 'latest.json'
        if not latest.exists():
            return {'projectId': payload['projectId'], 'manifest': None, 'worldContext': None,
                    'nodes': 0, 'tasks': [], 'built': False, 'capabilities': capabilities,
                    'runtimeExecution': {'python': sys.executable, 'prefix': sys.prefix,
                        'controllerOutputContract': 'separate-logs-v1'}}
        revision = json.loads(latest.read_text())['revision']
    directory = within(project / 'revisions', revision)
    manifest = json.loads((directory / 'manifest.json').read_text())
    capabilities = {**manifest.get('capabilities', {}), **robot_capabilities(manifest.get('io'), current_runtime)}
    world = World.restore(json.loads((directory / 'scene.json').read_text()))
    scope = payload.get('nodeId') or payload.get('nodeScope')
    return {**manifest, 'manifest': manifest, 'worldContext': world.context(scope),
            'treeReport': world.validate(), 'nodes': len(world.nodes), 'tasks': manifest['tasks'],
            'built': True, 'runtimeCompatible': manifest['runtime'] == current_runtime,
            'capabilities': capabilities}


@measure('observe')
def _episode_frame(episode, trajectory, camera_id=None, frame_index=None):
    recording = episode['recording']
    camera_id = camera_id if camera_id is not None else recording['primaryCameraId']
    declaration = next((row for row in recording['cameras'] if row['cameraId'] == camera_id), None)
    if declaration is None:
        raise ValueError('camera does not belong to the frozen episode')
    candidates = [row for row in trajectory['frames'] if row['cameraId'] == camera_id]
    if not candidates:
        raise ValueError('selected camera has no actual frame')
    index = candidates[-1]['index'] if frame_index is None else frame_index
    if type(index) is not int or not 0 <= index < len(trajectory['frames']):
        raise ValueError('unknown global camera frame')
    frame = trajectory['frames'][index]
    if frame['index'] != index or frame['cameraId'] != camera_id:
        raise ValueError('global frame does not belong to the selected camera')
    camera = {**declaration, 'scope': frame['cameraScope'], 'primPath': frame['cameraPrimPath'],
              'worldFromCamera': frame['worldFromCamera'], 'intrinsics': frame['intrinsics'],
              'coordinateSpec': frame['coordinateSpec'], 'viewMode': frame.get('viewMode', 'full_geometry'),
              'hiddenVisualNodeIds': frame.get('hiddenVisualNodeIds', []),
              'visibilityScope': frame.get('visibilityScope', 'camera_framing; occlusion is retained')}
    return frame, camera


def _observation_camera(manifest, world, scope, camera_id, *, stage=None):
    """Select one frozen sensor, or an explicit node/empty-recording preview."""
    from runtime.simulation import camera_for
    if scope is not None and scope not in world.nodes:
        raise ValueError('unknown observation node')
    if scope is not None and camera_id is not None:
        raise ValueError('node framing and a frozen camera cannot be selected together')
    task = Task(**manifest['tasks'][0])
    camera_id = camera_id if camera_id is not None or scope is not None else task.recording['primaryCameraId']
    if camera_id is not None:
        cameras = manifest['compiled'].get('recordingCameras', {}).get(task.id, {})
        declaration = next((row for row in task.recording['cameras'] if row['cameraId'] == camera_id), None)
        if declaration is None or camera_id not in cameras:
            raise ValueError('camera does not belong to the first frozen task')
        camera = cameras[camera_id]
        if any(camera.get(key) != value for key, value in declaration.items()):
            raise ValueError('compiled observation camera differs from the frozen declaration')
    else:
        camera = camera_for(world, scope=scope, stage=stage) if scope is not None else manifest['camera']
    return task, camera, camera_id


def _observation_ids(payload):
    if 'cameraIds' not in payload:
        return None
    ids = payload['cameraIds']
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 4
            or any(not isinstance(name, str) or not name for name in ids) or len(set(ids)) != len(ids)):
        raise ValueError('cameraIds requires one to four distinct frozen camera IDs')
    for name in ids:
        valid_id(name)
    if any(payload.get(name) is not None for name in ('cameraId', 'nodeId', 'nodeScope', 'episode', 'frame')):
        raise ValueError('cameraIds cannot be combined with a single camera, node, episode or frame')
    return ids


def _observation_batch_clock(frames):
    from runtime.records import _clock
    clocks = []
    for frame in frames:
        _clock(frame)
        clocks.append({key: frame[key] for key in ('stageId', 'physicsStep', 'time', 'simulationTime', 'timeOrigin')})
    if not clocks or any(clock != clocks[0] for clock in clocks[1:]):
        raise ValueError('batch observation frames do not share one actual physical clock')


def _observation_result(manifest, world, scope, checked, frame, camera, path, root, content=None):
    content = path.read_bytes() if content is None else content
    camera = {**camera, **{key:deepcopy(frame[key]) for key in ('worldFromCamera','intrinsics','coordinateSpec')}}
    return {'revision': manifest['revision'], 'nodeScope': scope, 'requestedScope': scope, 'checkedScope': checked,
            'cameraId': frame.get('cameraId'), 'globalFrameIndex': frame.get('index'), 'frameIndex': frame.get('frameIndex'),
            'cameraScope': frame.get('cameraScope', camera['scope']), 'cameraPrimPath': camera.get('primPath'),
            'camera': camera, 'viewMode': camera.get('viewMode', 'full_geometry'),
            'hiddenVisualNodeIds': camera.get('hiddenVisualNodeIds', []),
            'visibilityScope': camera.get('visibilityScope', 'camera_framing; occlusion is retained'),
            'artifactRef': str(path.relative_to(root)), 'image': {'data': base64.b64encode(content).decode(), 'mimeType': 'image/png'},
            'worldContext': world.context(scope) if scope else world.context(), 'nodes': len(world.nodes)}


def _web_preview_inputs(payload, root):
    """Read published frozen bytes without loading its programs or physical runtime."""
    if any(payload.get(key) is not None for key in ('cameraIds', 'cameraId', 'nodeId', 'nodeScope', 'episode', 'episodeId', 'frame')):
        raise ValueError('web preview cannot mix with camera, node or episode selection')
    project = root/'projects'/valid_id(payload['projectId'])
    directory = within(project/'revisions', valid_id(payload['revision']))
    manifest_path = directory/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    validate_protocol(manifest, 'sceneagent-build-v6')
    if (manifest.get('status') != 'completed' or manifest.get('projectId') != payload['projectId']
            or manifest.get('revision') != payload['revision']
            or manifest.get('artifacts', {}).get('manifest') != str(manifest_path.relative_to(root))):
        raise ValueError('web preview requires the requested published frozen revision')
    validate_publication(manifest, root)
    locked = {}
    for group in ('assets', 'frozenArtifactDigests', 'sourceDigests'):
        for name, expected in manifest[group].items():
            if name in locked and locked[name] != expected:
                raise ValueError('web preview frozen file has conflicting digests: '+name)
            locked[name] = expected
    files = {manifest_path}
    for name, expected in locked.items():
        path = within(directory, name)
        if not path.is_file() or digest(path) != expected:
            raise ValueError('web preview frozen source or dependency digest mismatch: '+name)
        files.add(path)
    compiled = manifest['compiled']
    usd_path = within(directory, compiled['usd'])
    if (manifest['frozenArtifactDigests'].get(compiled['usd']) != compiled['frozenUsdSha256']
            or digest(usd_path) != compiled['frozenUsdSha256']):
        raise ValueError('web preview USD source is not frozen')
    audit_name = compiled['nativeStageAudit']
    if audit_name not in manifest['frozenArtifactDigests']:
        raise ValueError('web preview native stage audit is not frozen')
    audit = json.loads(within(directory, audit_name).read_text())
    if (audit.get('schema') not in ('sceneagent-native-stage-audit-v1', 'sceneagent-native-stage-audit-v2') or audit.get('entry') != compiled['usd']
            or not isinstance(audit.get('files'), dict) or compiled['usd'] not in audit['files']
            or any(locked.get(name) != checksum for name, checksum in audit['files'].items())):
        raise ValueError('web preview native stage closure differs from the frozen manifest')
    from runtime.mdl import verify as verify_mdl
    verify_mdl(audit, directory)
    source = {'manifestRef': str(manifest_path.relative_to(root)), 'manifestSha256': digest(manifest_path),
              'usdRef': str(usd_path.relative_to(root)), 'usdSha256': digest(usd_path)}
    return project, directory, manifest, source, files


def _web_preview(payload, root):
    from runtime.simulation import export_preview
    project, directory, manifest, source, _ = _web_preview_inputs(payload, root)
    material_audit = json.loads(within(directory, manifest['compiled']['nativeStageAudit']).read_text())
    if material_audit.get('mdl') is not None:
        from runtime.simulation import configure_native_materials
        configure_native_materials(directory, material_audit)
    converter = runtime_key()
    output = project/'previews'/manifest['revision']/converter/valid_id(payload['requestId'])
    output.mkdir(parents=True, exist_ok=False)
    progress('web_preview', '正在从冻结场景生成网页预览', revision=manifest['revision'])
    export_preview(within(directory, manifest['compiled']['usd']), output/'preview.glb', manifest['compiled']['bindings'])
    if _web_preview_inputs(payload, root)[3] != source:
        raise ValueError('web preview source changed during conversion')
    artifacts = {'preview': str((output/'preview.glb').relative_to(root)),
                 'previewEvidence': str((output/'preview-evidence.json').relative_to(root)),
                 'rawPreview': str((output/'preview-raw.glb').relative_to(root))}
    result = {'schema': 'sceneagent-web-preview-v1', 'projectId': payload['projectId'],
              'revision': manifest['revision'], 'checkedScope': 'web_preview', 'source': source,
              'converterRuntime': converter, 'artifacts': artifacts,
              'digests': {name: digest(within(root, reference)) for name, reference in artifacts.items()},
              'artifactRef': str((output/'receipt.json').relative_to(root))}
    save(output/'receipt.json', result)
    return result


def _web_preview_files(payload, result, root):
    project, _, manifest, source, files = _web_preview_inputs(payload, root)
    converter = runtime_key()
    output = project/'previews'/manifest['revision']/converter/valid_id(payload['requestId'])
    artifacts = {name: str((output/filename).relative_to(root)) for name, filename in (
        ('preview', 'preview.glb'), ('previewEvidence', 'preview-evidence.json'), ('rawPreview', 'preview-raw.glb'))}
    receipt = output/'receipt.json'
    if (result.get('schema') != 'sceneagent-web-preview-v1' or result.get('checkedScope') != 'web_preview'
            or result.get('projectId') != payload['projectId'] or result.get('revision') != manifest['revision']
            or result.get('source') != source or result.get('converterRuntime') != converter
            or result.get('artifacts') != artifacts or result.get('artifactRef') != str(receipt.relative_to(root))
            or result.get('digests') != {name: digest(within(root, reference)) for name, reference in artifacts.items()}
            or json.loads(receipt.read_text()) != result):
        raise ValueError('web preview result differs from its frozen source or derived files')
    evidence = json.loads(within(root, artifacts['previewEvidence']).read_text())
    if (evidence.get('sourceUsdSha256') != source['usdSha256']
            or evidence.get('previewSha256') != result['digests']['preview']
            or evidence.get('rawGlbSha256') != result['digests']['rawPreview']
            or evidence.get('originalBinaryPreservedAsPrefix') is not True
            or not isinstance(evidence.get('visibleMeshes'), int) or evidence['visibleMeshes'] <= 0):
        raise ValueError('web preview conversion evidence differs from its source or outputs')
    return files | {receipt, *(within(root, reference) for reference in artifacts.values())}


def observe(payload, root):
    if payload.get('output') == 'web_preview':
        return _web_preview(payload, root)
    camera_ids = _observation_ids(payload)
    project, directory, manifest, world = frozen(root, payload)
    if camera_ids is not None:
        cameras = {}
        for name in camera_ids:
            task, camera, _ = _observation_camera(manifest, world, None, name)
            cameras[name] = camera
        output = directory/'observations'
        output.mkdir(exist_ok=True)
        env = load_environment(directory, manifest, world, output/'scratch', task, record=False, cameras=cameras)
        try:
            frames = [env.latest_camera_frames.get(name) for name in camera_ids]
            if any(not isinstance(frame, dict) or 'path' not in frame for frame in frames):
                raise ValueError('selected batch camera has no actual frame; see retained sensor diagnostics')
            _observation_batch_clock(frames)
            observations = []
            request_id = valid_id(payload.get('requestId') or uuid.uuid4().hex[:16])
            for name, frame in zip(camera_ids, frames):
                path = output/(request_id+'_'+name+'.png')
                shutil.copy2(within(output/'scratch', frame['path']), path)
                save(path.with_suffix('.camera.json'), frame)
                observations.append(_observation_result(manifest, world, None, 'rendered_camera_frame',
                    frame, env.camera_specs[name], path, root))
        finally:
            env.close()
        return {'revision': manifest['revision'], 'cameraIds': camera_ids, 'observations': observations,
            'worldContext': world.context(), 'nodes': len(world.nodes)}
    scope, camera_id = payload.get('nodeId') or payload.get('nodeScope'), payload.get('cameraId')
    if scope is not None and scope not in world.nodes:
        raise ValueError('unknown observation node')
    if scope is not None and camera_id is not None:
        raise ValueError('node framing and a frozen camera cannot be selected together')
    if payload.get('episode'):
        if scope is not None:
            raise ValueError('recorded episode frames cannot be reframed by node')
        output = project/'episodes'/valid_id(payload['episode'])
        episode = json.loads((output/'manifest.json').read_text())
        validate_publication(episode, root)
        directory, manifest, world, _ = robot_environment(root, project, directory, manifest, world, episode)
        if episode['revision'] != manifest['revision']:
            raise ValueError('episode belongs to a different revision')
        trajectory = json.loads((output/'trajectory.json').read_text())
        frame, camera = _episode_frame(episode, trajectory, camera_id, payload.get('frame'))
        path = within(output, frame['path'])
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != frame['sha256']:
            raise ValueError('recorded camera pixels changed')
        checked = 'recorded_camera_frame'
    else:
        output = directory/'observations'
        output.mkdir(exist_ok=True)
        stage = None
        if scope is not None and world.native:
            from pxr import Usd
            stage = Usd.Stage.Open(str(directory/manifest['compiled']['usd']))
        task, camera, camera_id = _observation_camera(manifest, world, scope, camera_id, stage=stage)
        env = load_environment(directory, manifest, world, output/'scratch', task, record=False, camera=camera)
        try:
            frame, camera = env.latest_camera, env.camera_spec
            source = within(output/'scratch', frame['path'])
            name = valid_id(payload.get('requestId') or uuid.uuid4().hex[:16])
            path = output/(name+'_'+(camera_id or scope or 'whole_world')+'.png')
            shutil.copy2(source, path)
            save(path.with_suffix('.camera.json'), frame)
        finally:
            env.close()
        content, checked = path.read_bytes(), 'rendered_camera_frame'
    return _observation_result(manifest, world, scope, checked, frame, camera, path, root, content)


def _case_matches(expected, result):
    return isinstance(result, dict) and all(key in result and _same_json(value, result[key]) for key, value in expected.items())


@measure('verifyMedia')
def _verify_media(directory, manifest, trajectory, *, root, world=None, task=None, _transitions=None):
    """Check the one frame table against committed states and frozen sensors."""
    import imageio.v2 as imageio
    import numpy as np
    from runtime.records import read_transitions, validate_camera_frames
    from runtime.scene import validate_recording
    from runtime.simulation import recording_camera_pose, recording_camera_path, camera_intrinsics
    root, directory = Path(root).resolve(), Path(directory).resolve()
    if not directory.is_relative_to(root):
        raise ValueError('episode media directory leaves the fixed output root')
    task = task if task is not None else Task(**trajectory['task'])
    recording = validate_recording(manifest['recording'], world)
    if task.recording != recording or trajectory.get('recording') != recording:
        raise ValueError('episode recording differs from the frozen task')
    declarations = {row['cameraId']: row for row in recording['cameras']}
    failures = manifest.get('cameraFailures', {})
    if not isinstance(failures, dict) or set(failures)-set(declarations) or any(not isinstance(value, str) or not value for value in failures.values()):
        raise ValueError('invalid camera failure disclosure')
    if trajectory.get('cameraFailures', {}) != manifest.get('cameraFailures', {}):
        raise ValueError('camera failure disclosure differs from the episode')
    for camera_id, row in declarations.items():
        if row['modalities'] != ['rgb'] and (row['required']
                or not manifest.get('cameraFailures', {}).get(camera_id)
                or any(frame.get('cameraId') == camera_id for frame in trajectory['frames'])):
            raise ValueError('declared camera modality is unsupported by this runtime')
    rows, index, _ = read_transitions(directory) if _transitions is None else _transitions
    if index.get('recording') != recording:
        raise ValueError('recorded sensor contract differs from the episode')
    initial = index['initialSnapshot']['truth']
    states = {initial['physicsStep']: initial}
    for row in rows:
        state = row['truthAfter']
        if state['physicsStep'] in states:
            raise ValueError('duplicate committed camera state clock')
        states[state['physicsStep']] = state
    frame_index, by_camera, files = {}, {name: [] for name in declarations}, set()
    for position, frame in enumerate(trajectory['frames']):
        camera_id = frame.get('cameraId')
        if camera_id not in declarations or frame.get('index') != position:
            raise ValueError('episode frame camera or global identity mismatch')
        declaration = declarations[camera_id]
        if (frame.get('cameraScope') != 'task:'+task.id
                or frame.get('cameraPrimPath') != recording_camera_path(task.id, camera_id)):
            raise ValueError('camera identity differs from its frozen task and sensor')
        view_frames = by_camera[camera_id]
        step = frame.get('physicsStep')
        if (type(step) is not int or frame.get('sampleId') != step
                or step not in states or frame.get('frameIndex') != len(view_frames)):
            raise ValueError('episode camera lacks its committed state or view index')
        state = states[step]
        interval = round(index['io']['clock']['physicsRateHz']/declaration['rateHz'])
        if step != len(view_frames)*interval:
            raise ValueError('camera frame clock is not a complete regular prefix')
        if frame.get('stageId') != state['stageId'] or frame.get('timeOrigin') != state['timeOrigin']:
            raise ValueError('camera frame clock identity differs from actual committed state')
        for key in ('simulationTime', 'time'):
            if type(frame.get(key)) not in (int, float) or abs(frame[key]-state[key]) > 1e-7:
                raise ValueError('camera frame clock differs from actual committed state')
        expected_pose = recording_camera_pose(world, declaration, state=state,
            io=index['io'], array_directory=directory)
        actual_pose = np.asarray(frame.get('worldFromCamera'))
        if actual_pose.shape != (4, 4) or not np.allclose(actual_pose, expected_pose, atol=1e-6, rtol=0):
            raise ValueError('camera dynamic extrinsics differ from the actual mount state')
        width, height = declaration['resolution']
        expected_intrinsics = camera_intrinsics(width, height)
        if frame.get('intrinsics') != expected_intrinsics:
            raise ValueError('camera intrinsics differ from its frozen lens and resolution')
        expected_path = f"frames/{camera_id}/{len(view_frames):06d}.png"
        if frame.get('path') != expected_path:
            raise ValueError('camera pixel path differs from its view and frame identity')
        path = within(directory, expected_path)
        if digest(path) != frame.get('sha256') or imageio.imread(path).shape != (height, width, 3):
            raise ValueError('episode camera frame pixels or digest mismatch')
        files.add(path)
        frame_index[position] = frame
        view_frames.append(frame)
    camera_permission = task.measurements.get('camera', {})
    policy_ids = camera_permission.get('cameraIds', []) if camera_permission.get('visibleToPolicy', False) else []
    for snapshot in [index['initialSnapshot'], *[{'observation': row['nextObservation'], 'truth': row['truthAfter']} for row in rows]]:
        validate_camera_frames(snapshot['truth'], recording, frame_index,
            physics_rate_hz=index['io']['clock']['physicsRateHz'])
        validate_camera_frames(snapshot['observation'], recording, frame_index, camera_ids=policy_ids,
            physics_rate_hz=index['io']['clock']['physicsRateHz'])
        expected_references = {}
        for name, declaration in declarations.items():
            slot = snapshot['truth']['physicsStep']//round(index['io']['clock']['physicsRateHz']/declaration['rateHz'])
            if slot < len(by_camera[name]):
                expected_references[name] = by_camera[name][slot]
            elif name in failures:
                expected_references[name] = {'cameraId': name, 'status': 'unavailable', 'reason': failures[name]}
            else:
                raise ValueError('camera is missing its latest committed sample or failure reason')
        if snapshot['truth'].get('cameraFrames', {}) != expected_references:
            raise ValueError('truth camera references differ from the latest actual samples')
        expected_policy = {name: frame for name, frame in expected_references.items() if name in policy_ids}
        if snapshot['observation'].get('cameraFrames', {}) != expected_policy:
            raise ValueError('policy camera references differ from the latest authorized samples')
    expected_videos = {'video:'+name: str((directory/f'videos/{name}.mp4').relative_to(root))
        for name, declaration in declarations.items() if declaration['video'] and by_camera[name]}
    actual_videos = {key: value for key, value in manifest['artifacts'].items() if key.startswith('video:')}
    if actual_videos != expected_videos:
        raise ValueError('video artifact differs from the current episode recorded camera scope')
    expected_media = media_summary(manifest, trajectory['frames'])
    if manifest.get('media') != expected_media or trajectory.get('media') != expected_media:
        raise ValueError('episode media summary differs from the unique frame table')
    for camera_id, declaration in declarations.items():
        frames = by_camera[camera_id]
        expected_count = 1+index['physicalSteps']//round(index['io']['clock']['physicsRateHz']/declaration['rateHz'])
        reason = manifest.get('cameraFailures', {}).get(camera_id)
        if len(frames) != expected_count:
            if declaration['required']:
                raise ValueError('required camera has missing frames')
            if not isinstance(reason, str) or not reason:
                raise ValueError('optional camera missing frames requires an explicit reason')
        if not frames:
            if declaration['required']:
                raise ValueError('required camera has no actual frames')
            continue
        if declaration['video']:
            filename = f'videos/{camera_id}.mp4'
            path = within(directory, filename)
            reference = manifest['artifacts'].get('video:'+camera_id)
            if reference != str((directory/filename).relative_to(root)):
                raise ValueError('video artifact differs from the current episode camera scope')
            with imageio.get_reader(path, format='ffmpeg') as video:
                metadata = video.get_meta_data()
                width, height = declaration['resolution']
                if (tuple(metadata['size']) != (width, height)
                        or abs(metadata['fps']-declaration['rateHz']) > 1e-6
                        or video.count_frames() != len(frames)
                        or video.get_data(0).shape != (height, width, 3)):
                    raise ValueError('episode video pixels or camera clock mismatch')
            files.add(path)
    primary = recording['primaryCameraId']
    expected_primary = manifest['artifacts'].get('video:'+primary) if primary else None
    if manifest['artifacts'].get('video') != expected_primary:
        raise ValueError('primary video alias differs from its camera artifact')
    return files


def media_summary(manifest, frames):
    """A lightweight projection, never a second media-state authority."""
    recording = manifest['recording']
    failures = manifest.get('cameraFailures', {})
    cameras = []
    for declaration in recording['cameras']:
        camera_id = declaration['cameraId']
        captured = [row for row in frames if row['cameraId'] == camera_id]
        reason = failures.get(camera_id)
        status = ('partial' if captured else 'unavailable') if reason else ('available' if captured else 'unavailable')
        row = {'cameraId': camera_id, 'required': declaration['required'], 'status': status,
               'video': manifest['artifacts'].get('video:'+camera_id) if captured else None,
               'frameCount': len(captured), 'firstTime': captured[0]['time'] if captured else None,
               'lastTime': captured[-1]['time'] if captured else None}
        if reason:
            row['reason'] = reason
        cameras.append(row)
    return {'primaryCameraId': recording['primaryCameraId'], 'cameras': cameras}


def verify_episode(root, directory, build_directory, build, world, *, selected=None):
    """Parent-readable source/facts checks; never trust a saved grading state."""
    root, directory = Path(root).resolve(), Path(directory).resolve()
    from runtime.records import read_transitions, execution_counts, array_references, read_array, _plain_file_identity
    manifest = json.loads((directory/'manifest.json').read_text())
    validate_protocol(manifest, 'sceneagent-episode-v6')
    setup_required = set()
    if manifest.get('robotSetupRef'):
        if not build.get('_robotSetup'):
            build_directory, build, world, setup_required = robot_environment(root, root/'projects'/build['projectId'],
                build_directory, build, world, manifest)
        binding = build['_robotSetup']
        path = within(root, binding['robotSetupRef'])
        if (any(manifest.get(key) != value for key,value in binding.items())
                or digest(path) != binding['robotSetupSha256']):
            raise ValueError('episode robot installation digest differs from its frozen input')
        setup_required.add(path)
    if manifest['io'] != build['io'] or build['io'] != build['compiled']['io']:
        raise ValueError('episode I/O differs from the actual frozen environment')
    actuated = has_control_ports(build['io'])
    if (manifest.get('executionKind') != ('actuator_physics_native' if actuated else 'native_physics')
            or not actuated and (manifest.get('candidateDemonstrationEligible') or manifest.get('demonstrationEligible'))):
        raise ValueError('robot demonstration requires actual frozen control ports and matching actuator execution kind')
    task = next((Task(**row) for row in build['tasks'] if row['id'] == manifest['taskId']), None)
    if task is None or manifest['revision'] != build['revision'] or manifest['projectId'] != build['projectId']:
        raise ValueError('episode does not belong to the frozen environment task')
    files = frozen_sources(build_directory, build)
    grader = rubric_sources(task, files, build['sourceDigests'])
    descriptor = {key: grader[key] for key in ('sourceRef', 'sourceDigest', 'helperDigests')}
    if manifest.get('rubric') != descriptor:
        raise ValueError('episode rubric source differs from its frozen task')
    if selected is not None and any(manifest.get(field) != selected[key] for field, key in (
            ('controllerSource', 'sourceRef'), ('controllerDigest', 'sourceDigest'), ('controllerHelperDigests', 'helperDigests'))):
        raise ValueError('episode controller differs from the independently frozen source')
    report = json.loads((directory/'report.json').read_text())
    if (build.get('runtimeExecution', {}).get('workerTimeoutParameter') == 'workerTimeoutSeconds'
            or 'workerTimeoutSeconds' in manifest or 'workerTimeoutSeconds' in report):
        if (any('workerTimeoutSeconds' not in value for value in (manifest, report))
                or worker_timeout(manifest) != worker_timeout(report)):
            raise ValueError('episode manifest/report timeout budget differs')
    for label, filename in (('manifest', 'manifest.json'), ('report', 'report.json'), ('records', 'records.json'),
                            ('transitions', 'transitions.jsonl'), ('trajectory', 'trajectory.json'), ('annotations', 'annotations.json')):
        if manifest['artifacts'].get(label) != str((directory/filename).relative_to(root)):
            raise ValueError('episode artifact reference differs from the fixed output scope')
    if manifest.get('manifestRef') != build['artifacts']['manifest']:
        raise ValueError('episode environment source reference differs from its frozen build')
    required = {directory/'manifest.json', directory/'report.json'} | setup_required
    control = manifest.get('control')
    if report.get('executionStatus') in ('completed', 'truncated') and (not isinstance(control, dict)
            or control.get('isolated') is not True or control.get('kind') != manifest['controllerKind']
            or control.get('policyDigest') != manifest['controllerDigest']
            or control.get('openCalls', 0) < 1 or control.get('resetCalls', 0) < 1):
        raise ValueError('normal episode lacks the matching isolated controller execution')
    if report.get('initializationFailed'):
        if report.get('result') is not None or report.get('scoreStatus') != 'unavailable' or not report.get('error'):
            raise ValueError('initialization failure cannot publish a task score')
        required.update(directory/name for name in ('records.json', 'transitions.jsonl', 'native-step-failure.json', 'camera-failure.json') if (directory/name).is_file())
        required.update(directory.glob('frames/**/*.png'))
        required.update(directory.glob('videos/*.mp4'))
        required.update(directory.glob('arrays/*.npy'))  # Diagnostic constructor output, never a committed sample.
        return report, required
    required.update(directory/name for name in ('records.json', 'transitions.jsonl', 'trajectory.json'))
    witnesses, references = {}, {}
    with measure('verifyRecords'):
        for label, name in (('records','records.json'), ('transitions','transitions.jsonl')):
            path, identity = _plain_file_identity(directory, name)
            expected = manifest.get('artifactDigests', {}).get(label)
            checksum = digest(path)
            if expected is not None and checksum != expected:
                raise ValueError('episode artifact digest mismatch')
            if _plain_file_identity(directory, name)[1] != identity:
                raise ValueError('episode file changed before record validation: '+name)
            witnesses[name] = (identity, checksum)
        transitions = read_transitions(directory)
    rows, index, tail = transitions

    @measure('verifyArrayFiles')
    def include_array(reference):
        name = reference['ref']
        if name in references:
            if references[name] != reference:
                raise ValueError('conflicting array reference for one path')
            return
        path, identity = _plain_file_identity(directory, name)
        read_array(directory, reference)
        references[name] = deepcopy(reference)
        witnesses[name] = (identity, reference['sha256'])
        required.add(path)

    for facts in (index['initialSnapshot'], *(row['facts'] for row in rows)):
        for reference in array_references(facts):
            include_array(reference)
    if (index.get('task') != {key: asdict(task)[key] for key in ('id', 'instruction', 'bindings', 'measurements')}
            or index.get('recording') != task.recording
            or manifest.get('recording') != task.recording
            or index.get('io') != manifest.get('io')):
        raise ValueError('episode task, sensor permissions or specifications differ from the frozen contract')
    grade = recompute_rubric(task, grader, directory, io=build['io'], _transitions=transitions)
    if grade['scoreStatus'] != report.get('committedScoreStatus') or not _same_json(grade['result'], report.get('lastCommittedResult')):
        raise ValueError('episode score differs from independently replayed frozen rubric')
    if not _same_json(manifest.get('lastCommittedResult'), grade['result']):
        raise ValueError('episode manifest prefix score differs from committed facts')
    actual_steps = manifest.get('totalExecutedPhysicalSteps')
    failure_path = directory/'native-step-failure.json'
    if failure_path.is_file():
        failure = json.loads(failure_path.read_text())
        if (failure.get('countStatus') == 'known' and failure.get('totalExecutedPhysicalSteps') != actual_steps
                or failure.get('countStatus') != 'known' and actual_steps is not None):
            raise ValueError('native failure count differs from its actual SDK clock')
        for reference in array_references(failure.get('actualTail')):
            include_array(reference)
    if actual_steps is None:
        if report.get('executionStatus') != 'environment_error':
            raise ValueError('unknown physical execution cannot publish a normal task result')
    else:
        counts = execution_counts(directory, actual_steps, _transitions=transitions)
        if any(manifest.get(key) != value or report.get(key) != value for key, value in counts.items()):
            raise ValueError('episode execution boundary differs from committed records')
        if (counts['uncommittedPhysicalSteps'] or tail) and report.get('executionStatus') == 'completed':
            raise ValueError('normal episode has incomplete actual execution')
    final_available = report.get('executionStatus') not in ('environment_error', 'score_unavailable')
    if report.get('resultScope') != ('actual_committed_execution' if final_available else 'diagnostic_prefix_only'):
        raise ValueError('episode grade scope differs from its actual execution boundary')
    if report.get('scoreStatus') != ('available' if final_available else 'unavailable'):
        raise ValueError('final grade availability differs from actual execution status')
    if final_available:
        if not _same_json(report.get('result'), grade['result']) or any(not _same_json(report.get(key), value) for key, value in grade['result'].items()):
            raise ValueError('public grade aliases differ from generated result')
    elif report.get('result') is not None or report.get('success') is not None or report.get('reward') is not None:
        raise ValueError('invalid actual execution cannot publish its prefix grade as a final result')
    if not _same_json(manifest.get('result'), report.get('result')):
        raise ValueError('episode manifest result differs from the committed generated grade')
    if manifest.get('terminated') != index['terminated'] or manifest.get('truncated') != index['truncated']:
        raise ValueError('episode termination differs from its committed generated result')
    if grade['scoreStatus'] == 'unavailable' and report.get('executionStatus') != 'score_unavailable':
        raise ValueError('unavailable grading cannot be upgraded to task success/failure')
    trajectory = json.loads((directory/'trajectory.json').read_text())
    if (trajectory.get('schema') != 'sceneagent-studio-trajectory-v6'
            or any(trajectory.get(key) != manifest.get(key) for key in ('robotSetupRef','robotSetupSha256'))
            or trajectory.get('io') != build['io'] or trajectory.get('initialState') != index['initialSnapshot']['truth']
            or manifest.get('initialState') != index['initialSnapshot']['truth']):
        raise ValueError('episode trajectory differs from its unique frozen record contract')
    if report.get('executionStatus') == 'environment_error':
        required.update(within(directory, frame['path']) for frame in trajectory['frames'] if within(directory, frame['path']).is_file())
    else:
        required.update(_verify_media(directory, manifest, trajectory, root=root, world=world, task=task,
            _transitions=transitions))
    with measure('verifyArtifactDigests'):
        for label, expected in manifest.get('artifactDigests', {}).items():
            path = within(root, manifest['artifacts'][label])
            relative = path.relative_to(directory).as_posix()
            if relative in witnesses:
                if witnesses[relative][1] != expected:
                    raise ValueError('episode artifact digest mismatch')
            elif digest(path) != expected:
                raise ValueError('episode artifact digest mismatch')
    from runtime.records import project_annotations
    if report.get('executionStatus') != 'environment_error':
        annotations = json.loads((directory/'annotations.json').read_text())
        expected_annotations = {'schema': 'sceneagent-generated-annotations-v1', 'taskId': task.id, 'rubric': descriptor,
            'bindings': task.bindings, 'annotations': project_annotations(rows, index['task'], descriptor)}
        if annotations != expected_annotations:
            raise ValueError('annotations differ from committed generated interpretation')
        required.add(directory/'annotations.json')
    for filename in ('native-step-failure.json', 'camera-failure.json'):
        if (directory/filename).is_file():
            required.add(directory/filename)
            for reference in array_references(json.loads((directory/filename).read_text())):
                include_array(reference)
    with measure('verifyFileSeal'):
        for name, (identity, expected) in witnesses.items():
            path, before = _plain_file_identity(directory, name)
            if before != identity or digest(path) != expected or _plain_file_identity(directory, name)[1] != identity:
                raise ValueError('episode file identity or digest changed during verification: '+name)
    return report, required


def validation_cases(root, project, build_directory, manifest, world, episode_ids, *, required=None):
    if not isinstance(episode_ids, list) or len(episode_ids) != len(set(episode_ids)):
        raise ValueError('validation episode IDs must be a unique list')
    files = frozen_sources(build_directory, manifest)
    by_case = {}
    phase = 'required_files:validation' if required is not None else 'validation'
    for position, id in enumerate(episode_ids):
        flush_performance(phase, progress={'episodeId': id, 'completed': position, 'total': len(episode_ids)})
        directory = project/'episodes'/valid_id(id)
        episode = json.loads((directory/'manifest.json').read_text())
        validate_publication(episode, root)
        if episode.get('purpose') != 'verification' or episode.get('controllerKind') != 'case':
            raise ValueError('validation requires an actual published verification case episode')
        task = next((Task(**row) for row in manifest['tasks'] if row['id'] == episode['taskId']), None)
        case = next((case for case in task.evaluation['cases'] if case['id'] == episode.get('caseId')), None) if task else None
        if case is None or episode.get('expected') != case['expected'] or episode.get('requirementRefs') != case['requirementRefs']:
            raise ValueError('verification case differs from the frozen generated declaration')
        selected = evaluation_sources(task, files, manifest['sourceDigests'])[case['id']]
        report, checked = verify_episode(root, directory, build_directory, manifest, world, selected=selected)
        if required is not None:
            required.update(checked)
        key = (task.id, case['id'])
        if key in by_case:
            raise ValueError('multiple validation episodes selected for one case')
        valid = report['executionStatus'] in ('completed', 'truncated') and report['scoreStatus'] == 'available'
        by_case[key] = {'id': case['id'], 'episodeId': id, 'projectId': manifest['projectId'], 'revision': manifest['revision'],
            'artifacts': {'manifest': episode['artifacts']['manifest']}, 'expected': case['expected'], 'result': report.get('result'),
            'requirementRefs': case['requirementRefs'], 'sourceRef': selected['sourceRef'], 'sourceDigest': selected['sourceDigest'],
            'executionStatus': report['executionStatus'], 'passed': bool(valid and _case_matches(case['expected'], report.get('result')))}
        flush_performance(phase, progress={'episodeId': id, 'completed': position+1, 'total': len(episode_ids)})
    tasks = []
    for definition in manifest['tasks']:
        task = Task(**definition)
        task.bind(world)
        grader = rubric_sources(task, files, manifest['sourceDigests'])
        cases = [by_case.get((task.id, case['id']), {'id': case['id'], 'episodeId': None,
            'expected': case['expected'], 'result': None, 'requirementRefs': case['requirementRefs'],
            'passed': False, 'executionStatus': 'not_run'}) for case in task.evaluation['cases']]
        from runtime.records import validate_io
        validate_io(manifest['io'])
        io_matches = _same_json(manifest['io'], manifest['compiled'].get('io'))
        tasks.append({'taskId': task.id, 'bindingPassed': True, 'measurementsPassed': io_matches,
            'passed': bool(cases) and io_matches and all(case['passed'] for case in cases),
            'rubric': {key: grader[key] for key in ('sourceRef', 'sourceDigest')}, 'cases': cases})
    return tasks


def check(payload, root):
    project, directory, manifest, world = frozen(root, payload)
    episode_ids = payload.get('episodeIds') or ([payload['episodeId']] if payload.get('episodeId') else [])
    rows = validation_cases(root, project, directory, manifest, world, episode_ids)
    ids = payload.get('taskIds') or [row['taskId'] for row in rows]
    if any(id not in {row['taskId'] for row in rows} for id in ids):
        raise ValueError('unknown task ID')
    rows = [row for row in rows if row['taskId'] in ids]
    report = {'revision': manifest['revision'], 'passed': bool(rows) and all(row['passed'] for row in rows),
              'tasks': rows, 'binding': world.validate(), 'nodeScope': payload.get('nodeScope'),
              'checkedScope': 'frozen_bindings_sources_and_explicit_generated_validation_episodes'}
    if payload.get('baseRevision'):
        base = World.restore(json.loads(within(project/'revisions', payload['baseRevision']).joinpath('scene.json').read_text()))
        report['difference'] = base.diff(world)
    path = directory/'task-check.json'
    save(path, report)
    return {**report, 'artifactRef': str(path.relative_to(root))}


def _consume_selection(payload):
    selected = payload.get('episodes')
    if not isinstance(selected, list) or not selected:
        raise ValueError('consumer requires explicit episode/task/use selections')
    seen = set()
    for row in selected:
        if (not isinstance(row, dict) or set(row) not in ({'episodeId', 'taskId', 'use'}, {'episodeId', 'taskId', 'use', 'revision'})
                or row['use'] not in ('execution_data', 'successful_demonstration')):
            raise ValueError('consumer episode selection requires episodeId, taskId and use')
        valid_id(row['episodeId']); valid_id(row['taskId'])
        if 'revision' in row:
            valid_id(row['revision'])
        if row['episodeId'] in seen:
            raise ValueError('duplicate consumer episode selection')
        seen.add(row['episodeId'])
    return selected


def _behavior_data(root, project, directory, manifest, world, selection):
    from runtime.records import load_batch
    output = project/'episodes'/selection['episodeId']
    episode = json.loads((output/'manifest.json').read_text())
    validate_publication(episode, root)
    directory, manifest, world, setup_files = robot_environment(root, project, directory, manifest, world, episode)
    if (episode['revision'] != manifest['revision'] or episode['taskId'] != selection['taskId']
            or episode.get('purpose') != 'demonstration' or episode.get('controllerKind') != 'expert'):
        raise ValueError('consumer behavior data source or version differs from its selection')
    task = next(Task(**row) for row in manifest['tasks'] if row['id'] == selection['taskId'])
    if 'behaviorRef' in episode:
        expert, behavior_files = recorded_behavior(root, project, episode)
    else:
        expert, behavior_files = expert_sources(task, frozen_sources(directory, manifest), manifest['sourceDigests']), set()
    report, required = verify_episode(root, output, directory, manifest, world, selected=expert)
    required.update(behavior_files | setup_files)
    batch = load_batch(output, manifest['io'])
    if (not has_control_ports(manifest['io']) or report['executionStatus'] not in ('completed', 'truncated')
            or report['scoreStatus'] != 'available' or batch['uncommittedTail'] or batch['physicalSteps'] <= 0):
        raise ValueError('consumer requires complete actual expert control data with available scoring')
    eligible = bool(report['executionStatus'] == 'completed' and report['success'] is True
                    and episode.get('control', {}).get('isolated') is True)
    if episode.get('demonstrationEligible') is not eligible:
        raise ValueError('behavior successful demonstration label differs from its actual result')
    if selection['use'] == 'successful_demonstration' and not eligible:
        raise ValueError('successful demonstration requires a true complete original success')
    summary = {**selection, 'projectId': manifest['projectId'], 'revision': manifest['revision'],
        **({'behaviorRef': episode['behaviorRef']} if 'behaviorRef' in episode else {}),
        **deepcopy(manifest.get('_robotSetup', {})),
        'artifacts': {'manifest': episode['artifacts']['manifest']}, 'demonstrationEligible': eligible, 'result': batch['lastResult'],
        'controlCycles': batch['controlCycles'], 'physicalSteps': batch['physicalSteps'],
        'streamNames': list(batch['io']['streams']), 'portNames': list(batch['io']['ports']),
        'status': batch['status'], 'uncommittedTail': batch['uncommittedTail'], 'rubric': batch['rubric'],
        'recordRef': episode['artifacts']['records'], 'transitionsRef': episode['artifacts']['transitions']}
    return summary, batch, required


def _interface_source(action, io):
    from runtime.records import validate_action
    validate_action(action, io)
    for name, spec in io['ports'].items():
        if name not in action and (spec['persistence'] != 'persistent' or spec.get('initialized') is not True):
            raise ValueError('interface source omits an uninitialized or nonpersistent port: '+name)
    value = json.dumps(action, sort_keys=True, allow_nan=False)
    source = ("import json\n\ndef run(env, task):\n"
        "    contract = env.open()\n    observation = env.observe()\n"
        "    action = json.loads("+repr(value)+")\n"
        "    period = 1/contract['io']['clock']['controlRateHz']\n"
        "    if observation['terminated']:\n"
        "        try:\n            env.step(action, period)\n"
        "        except RuntimeError as error:\n"
        "            if str(error) != 'ValueError: ended or failed native episode requires a fresh host process':\n                raise\n"
        "        else:\n            raise RuntimeError('terminated reset accepted an actuator step')\n"
        "    else:\n        env.step(action, period)\n    env.observe()\n")
    return {'sourceRef': 'interface_probe.py', 'sourceDigest': hashlib.sha256(source.encode()).hexdigest(),
            'helperDigests': {}, 'sourceFiles': {'interface_probe.py': source}}


def _interface_evidence(root, directory, manifest, world, output, source, batch, episode_ref, *, revision_scope=False):
    from runtime.records import read_transitions
    from runtime.robot import native_values
    location = output/'interfaces'
    if revision_scope:
        location = location/source['revision']
    location = location/source['taskId']
    source_file = location/'interface_probe.py'
    expected = _interface_source(batch['transitions'][0]['executedAction'], manifest['io'])
    if source_file.read_text() != expected['sourceFiles']['interface_probe.py']:
        raise ValueError('interface probe source differs from its original first executed action')
    probe = within(root, episode_ref)
    if probe.name != 'manifest.json' or not probe.is_relative_to(location/'episodes'):
        raise ValueError('interface episode leaves its fixed consumer task scope')
    episode = json.loads(probe.read_text())
    if (episode.get('taskId') != source['taskId'] or episode.get('purpose') != 'consumer_diagnostic' or episode.get('controllerKind') != 'candidate'
            or episode.get('demonstrationEligible') is not False
            or episode.get('candidateDemonstrationEligible') is not False):
        raise ValueError('interface probe cannot claim an expert demonstration')
    native_worker, native_files = None, set()
    if _environment_uses_mdl(directory, manifest):
        result_path, log_path = location/'native-interface-result.json', location/'native-interface.log'
        packet = _interface_sdk_packet(result_path)
        if (packet['episode'].get('revision') != manifest['revision'] or packet['episode'].get('taskId') != source['taskId']
                or packet['episode'].get('artifacts', {}).get('manifest') != str(probe.relative_to(root))):
            raise ValueError('native MDL interface SDK result differs from the selected episode')
        if log_path.is_symlink() or not log_path.is_file():
            raise ValueError('native MDL interface SDK log is missing or redirected')
        native_files.update((result_path, log_path))
        native_worker = {'resultRef':str(result_path.relative_to(root)), 'resultSha256':digest(result_path),
                         'logRef':str(log_path.relative_to(root)), 'logSha256':digest(log_path)}
    report, required = verify_episode(root, probe.parent, directory, manifest, world, selected=expected)
    required.update(native_files)
    rows, index, tail = read_transitions(probe.parent)
    if report['executionStatus'] not in ('completed', 'truncated') or report['scoreStatus'] != 'available' or tail:
        raise ValueError('interface probe has unavailable execution, recording or scoring')
    initialized = episode.get('nativeInitialTargets')
    if not isinstance(initialized, dict) or set(initialized) != set(manifest['io']['ports']):
        raise ValueError('interface lacks fresh native initialized targets')
    for name, spec in manifest['io']['ports'].items():
        native_values(initialized[name], spec['shape'], 'interface initialized target '+name)
        if spec.get('initialized') is not True:
            raise ValueError('interface native port was not initialized')
    ended = index['initialRubric']['result']['terminated']
    control = episode['control']
    if (control.get('openCalls', 0) < 1 or control.get('resetCalls', 0) < 1 or control.get('observeCalls', 0) < 1
            or control.get('actionAttempts') != 1 or control.get('actionCalls') != (0 if ended else 1)
            or control.get('rejectedActionCalls') != (1 if ended else 0)):
        raise ValueError('interface public API calls differ from its one-cycle probe source')
    cycles, ticks = (0, 0) if ended else (1, manifest['io']['clock']['physicsRateHz']//manifest['io']['clock']['controlRateHz'])
    if len(rows) != cycles or index['physicalSteps'] != ticks or report['totalExecutedPhysicalSteps'] != ticks:
        raise ValueError('interface probe did not record exactly its actual control cycle/ticks')
    if not ended:
        action = batch['transitions'][0]['executedAction']
        if rows[0]['targetAction'] != action or rows[0]['executedAction'] != action:
            raise ValueError('interface explicit commands differ from the original executed action')
    elif source['controlCycles'] <= 0 or source['physicalSteps'] <= 0:
        raise ValueError('initially terminated probe has no separate actual source step evidence')
    checks = episode.get('interfaceIsolationChecks')
    names = {'outputReadBlocked', 'outputWriteBlocked', 'frozenSourceWriteBlocked', 'runtimeWriteBlocked',
             'parentMemoryBlocked', 'parentDescriptorsBlocked', 'undeclaredSceneReadBlocked', 'networkBlocked'}
    if not isinstance(checks, dict) or set(checks) != names or any(value is not True for value in checks.values()):
        raise ValueError('interface isolation evidence is incomplete')
    required.add(source_file)
    row = {'taskId': source['taskId'], 'sourceEpisodeId': source['episodeId'],
        'sourceFactsDigest': batch['transitions'][0]['factsDigest'],
        'sourceRef': str(source_file.relative_to(root)), 'sourceDigest': expected['sourceDigest'],
        'episodeRef': str(probe.relative_to(root)), 'recordRef': episode['artifacts']['records'],
        'passed': True, 'controlCycles': cycles, 'physicalSteps': ticks, 'scoreAvailable': True,
        'coverage': {'open': 'checked', 'reset': 'checked', 'observe': 'checked', 'record': 'checked', 'score': 'checked',
                     'step': 'not_exercised' if ended else 'checked',
                     'endedStepRejection': 'checked' if ended else 'not_exercised'},
        'checkedScope': 'fresh_interface_smoke_only; source_episode_steps_are_separate_evidence'}
    if revision_scope:
        row.update(revision=manifest['revision'], sourceKind='validation_case', sourceCaseId=source['caseId'])
    if native_worker is not None:
        row['nativeInterface'] = native_worker
    return row, required, checks


def _consumed_environment(root, payload, original):
    """Load only the delivered new root, retaining the declared interpreter and native checks."""
    reference = payload.get('environmentRoot')
    if reference is None:
        return frozen(root, payload)
    relative = Path(reference)
    prefix = ('projects', payload['projectId'], 'deliveries', 'artifacts')
    if (relative.is_absolute() or '..' in relative.parts or relative.parts[:4] != prefix
            or len(relative.parts) < 6 or not re.fullmatch('[a-f0-9]{64}', relative.parts[4])):
        raise ValueError('exported environment must stay inside the frozen delivery closure')
    exported = within(root, reference)
    project, directory, manifest, world = frozen(exported, payload)
    if not _same_json(manifest, original) or any(Path(name).is_absolute() for name in manifest['assets']):
        raise ValueError('exported environment differs from the original frozen build or uses external assets')
    snapshot = within(exported, manifest['runtimeSnapshotRef'])/'runtime'
    source = Path(__file__).parent
    paths = [*source.glob('*.py'), *source.glob('motion-dependencies.json')]
    for file in paths:
        copied = snapshot/file.name
        if not copied.is_file() or copied.is_symlink() or digest(copied) != digest(file):
            raise ValueError('exported environment lacks the preserved frozen runtime')
    return project, directory, manifest, world


def _consumption_scope(payload):
    outputs = payload.get('requiredOutputs', ['generation', 'demonstrations', 'evaluation'])
    if (not isinstance(outputs, list) or any(not isinstance(value, str) for value in outputs)
            or len(outputs) != len(set(outputs)) or set(outputs) not in (
                {'generation', 'demonstrations', 'evaluation'}, {'generation', 'evaluation'}, {'demonstrations'})):
        raise ValueError('consumer requires a supported requiredOutputs selection')
    refs = payload.get('environmentRefs', [{'revision':payload['revision']}])
    if not isinstance(refs, list) or not refs:
        raise ValueError('consumer requires nonempty frozen environment references')
    seen = set()
    for ref in refs:
        if (not isinstance(ref, dict) or not {'revision'}.issubset(ref) or set(ref)-{'revision','split','taskIds'}
                or ref.get('split') is not None and ref['split'] not in ('train','validation','test')):
            raise ValueError('invalid consumer environment reference')
        valid_id(ref['revision'])
        if ref['revision'] in seen:
            raise ValueError('each revision requires one unique environment/split selection')
        seen.add(ref['revision'])
        if 'taskIds' in ref and (not isinstance(ref['taskIds'], list) or not ref['taskIds']
                or any(not isinstance(id, str) for id in ref['taskIds']) or len(set(ref['taskIds'])) != len(ref['taskIds'])):
            raise ValueError('environment task selection must be nonempty and unique')
    return outputs, refs


def _case_interface_source(root, project, directory, manifest, world, task_id, ids, *, required):
    from runtime.records import read_transitions, load_batch
    task = next(Task(**row) for row in manifest['tasks'] if row['id'] == task_id)
    files = frozen_sources(directory, manifest)
    for id in ids:
        output = project/'episodes'/valid_id(id)
        episode = json.loads((output/'manifest.json').read_text())
        if episode['revision'] != manifest['revision'] or episode['taskId'] != task_id:
            continue
        case = next((case for case in task.evaluation['cases'] if case['id'] == episode.get('caseId')), None)
        if (case is None or episode.get('controllerKind') != 'case' or episode.get('purpose') != 'verification'
                or episode.get('expected') != case['expected'] or episode.get('requirementRefs') != case['requirementRefs']):
            raise ValueError('interface source is not an actual frozen generated verification case')
        selected = evaluation_sources(task, files, manifest['sourceDigests'])[case['id']]
        validate_publication(episode, root)
        report, checked = verify_episode(root, output, directory, manifest, world, selected=selected)
        required.update(checked)
        transitions = read_transitions(output)
        if (report['executionStatus'] not in ('completed','truncated') or report['scoreStatus'] != 'available'
                or not transitions[0] or transitions[2]):
            continue
        batch = load_batch(output, manifest['io'], _transitions=transitions)
        return {'taskId':task_id, 'episodeId':id, 'revision':manifest['revision'], 'caseId':case['id'],
                'controlCycles':batch['controlCycles'], 'physicalSteps':batch['physicalSteps']}, batch
    raise ValueError('environment interface requires a verified same-revision/task case with actual executed steps')


def _environment_uses_mdl(directory, manifest):
    name = manifest.get('compiled', {}).get('nativeStageAudit')
    if name is None:
        return False
    path = within(Path(directory), name)
    if manifest.get('frozenArtifactDigests', {}).get(name) != digest(path):
        raise ValueError('consumer native audit is not frozen')
    return json.loads(path.read_text()).get('mdl') is not None


def consume_uses_mdl(payload):
    """Inspect every selected scope using JSON only, before the parent imports USD."""
    if payload.get('operation') != 'consume' or not payload.get('root') or not payload.get('projectId'):
        return False
    root = Path(payload['root']).resolve()
    project = root/'projects'/valid_id(payload['projectId'])
    revision = payload.get('revision')
    if revision is None:
        revision = json.loads((project/'latest.json').read_text())['revision']
    refs = payload.get('environmentRefs', [{'revision':revision}])
    for ref in refs:
        directory = within(project/'revisions', valid_id(ref['revision']))
        manifest = json.loads((directory/'manifest.json').read_text())
        if _environment_uses_mdl(directory, manifest):
            return True
    episode_ids = [row['episodeId'] for row in payload.get('episodes', [])]
    episode_ids += payload.get('validationEpisodes', [])
    for episode_id in episode_ids:
        episode = json.loads((project/'episodes'/valid_id(episode_id)/'manifest.json').read_text())
        if episode.get('robotSetupRef'):
            path = within(root, episode['robotSetupRef'])
            if path.name != 'manifest.json' or path.parent.parent != project/'robot-setups':
                raise ValueError('consumer installation leaves its project')
            if digest(path) != episode['robotSetupSha256']:
                raise ValueError('consumer installation manifest changed')
            if _environment_uses_mdl(path.parent, json.loads(path.read_text())['environment']):
                return True
    return False


def _consumer_interface_episode(root, project, directory, manifest, world, task, selected, kind, payload,
                                *, output_parent, interface_probes):
    if not _environment_uses_mdl(directory, manifest):
        return _episode(root, project, directory, manifest, world, task, selected, kind, payload,
                        output_parent=output_parent, interface_probes=interface_probes)
    from runtime.jobs import _token
    location = Path(output_parent).parent
    result_path = location/'native-interface-result.json'
    if result_path.exists() or (location/'native-interface.log').exists():
        raise ValueError('preserve the previous native MDL interface attempt')
    request = {'root':str(root), 'project':str(project), 'directory':str(directory),
        'manifest':manifest, 'world':world.snapshot(), 'task':asdict(task), 'selected':selected,
        'kind':kind, 'payload':payload, 'outputParent':str(output_parent), 'interfaceProbes':interface_probes,
        'parent':{'pid':os.getpid(), 'processToken':_token(os.getpid())}}
    portable = (Path(JOB_DIRECTORY)/'native-interfaces'/manifest['revision']/task.id
                if JOB_DIRECTORY is not None else location/'native-interface-kit')
    command = [sys.executable, str(Path(__file__).resolve()), '--native-interface-episode', str(result_path),
        '--portable', '--portable-root', str(portable)]
    command += [value for value in sys.argv if value.startswith('--/rtx/shaderDb/')]
    with (location/'native-interface.log').open('wb') as log:
        completed = subprocess.run(command, input=json.dumps(request).encode(), stdout=log,
            stderr=subprocess.STDOUT, timeout=worker_timeout(payload), close_fds=True)
    if completed.returncode or not result_path.is_file():
        raise ValueError('native MDL interface SDK process failed; see native-interface.log')
    packet = _interface_sdk_packet(result_path)
    episode = packet['episode']
    path = within(Path(root), episode['artifacts']['manifest'])
    if path.name != 'manifest.json' or not path.is_relative_to(Path(output_parent).resolve()):
        raise ValueError('native MDL interface result leaves its fixed output scope')
    return episode


def _interface_sdk_packet(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('native MDL interface SDK result is missing or redirected')
    packet = json.loads(path.read_text())
    if (not isinstance(packet, dict) or set(packet) != {'episode','termination'}
            or not isinstance(packet['episode'], dict) or not isinstance(packet['termination'], dict)):
        raise ValueError('native MDL interface SDK result is incomplete')
    termination = packet['termination']
    if (termination.get('applicationStarted') is not True or termination.get('replicatorCompleted') is not True
            or termination.get('terminationMode') != 'official_fast_shutdown'
            or termination.get('fullVendorCleanupVerified') is not False):
        raise ValueError('native MDL interface lacks a completed SDK termination barrier')
    return packet


def native_interface_entry(result_path):
    """Trusted SDK host; generated controller/rubric code keeps its existing sandboxes."""
    from runtime.jobs import _token
    request = json.load(sys.stdin)
    parent = request['parent']
    if sys.platform == 'linux':
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(1, 15, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'cannot bind interface SDK lifetime to parent')
    if os.getppid() != parent['pid'] or _token(parent['pid']) != parent['processToken']:
        raise RuntimeError('consumer parent ended before interface SDK initialization')
    root = Path(request['root']).resolve()
    payload = request['payload']
    project = root/'projects'/valid_id(payload['projectId'])
    if Path(request['project']).resolve() != project:
        raise ValueError('native interface project scope differs')
    directory = within(root, request['directory'])
    output = within(project/'consumer-reports', request['outputParent'])
    result_path = Path(result_path).resolve()
    if output.name != 'episodes' or result_path != output.parent/'native-interface-result.json':
        raise ValueError('native interface output leaves consumer scope')
    manifest = request['manifest']
    verify_inputs(manifest, runtime_key(), directory)
    snapshot = json.loads((directory/'scene.json').read_text())
    if not _same_json(snapshot, request['world']) or not any(_same_json(row, request['task']) for row in manifest['tasks']):
        raise ValueError('native interface world or task differs from frozen inputs')
    probes = [(name, f'/proc/{os.getpid()}/mem' if name == 'parentMemoryBlocked'
               else f'/proc/{os.getpid()}/fd/0' if name == 'parentDescriptorsBlocked' else path, write)
              for name,path,write in request['interfaceProbes']]
    from runtime import simulation
    code = 7
    try:
        episode = _episode(root, project, directory, manifest, World.restore(snapshot), Task(**request['task']),
            request['selected'], request['kind'], payload, output_parent=output, interface_probes=probes)
        termination = simulation.finish()
        save(result_path, {'episode':episode, 'termination':termination})
        code = 0
    finally:
        simulation.shutdown(code)
    sys.exit(code)


def _consume_scoped(payload, root, result=None):
    """Derive selected products from their own frozen revisions, for writing or parent verification."""
    outputs, refs = _consumption_scope(payload)
    project = root/'projects'/valid_id(payload['projectId'])
    directory = project/'consumer-reports'/valid_id(payload['requestId'])
    required, environments, pairs = set(), {}, set()
    for ref in refs:
        _, source, build, world = frozen(root, {**payload, 'revision':ref['revision']})
        ids = [row['id'] for row in build['tasks']]
        selected = ref.get('taskIds', ids)
        if set(selected)-set(ids) or 'evaluation' in outputs and set(selected) != set(ids):
            raise ValueError('consumer environment task coverage differs from the requested frozen tasks')
        pairs.update((ref['revision'], id) for id in selected)
        environments[ref['revision']] = (source, build, world, selected)
    if payload.get('revision') not in environments:
        raise ValueError('consumer primary revision is absent from its frozen environment selection')
    selections = _consume_selection(payload) if 'demonstrations' in outputs else []
    if 'demonstrations' not in outputs and payload.get('episodes'):
        raise ValueError('unrequested demonstration episodes cannot enter environment consumption')
    demonstrations, covered = [], set()
    for selection in selections:
        revision = selection.get('revision')
        if (revision, selection['taskId']) not in pairs:
            raise ValueError('behavior episode leaves the requested revision/task scope')
        source, build, world, _ = environments[revision]
        summary, _, checked = _behavior_data(root, project, source, build, world, selection)
        demonstrations.append(summary); covered.add((revision, selection['taskId'])); required.update(checked)
    if 'demonstrations' in outputs and covered != pairs:
        raise ValueError('required revision/task coverage lacks verified behavior data')
    ids = payload.get('validationEpisodes', [])
    if not isinstance(ids, list) or len(ids) != len(set(ids)):
        raise ValueError('validation episode IDs must be unique')
    if 'evaluation' not in outputs and ids:
        raise ValueError('unrequested evaluation episodes cannot enter data consumption')
    grouped = {revision:[] for revision in environments}
    for id in ids:
        episode = json.loads((project/'episodes'/valid_id(id)/'manifest.json').read_text())
        if (episode['revision'], episode['taskId']) not in pairs:
            raise ValueError('validation episode leaves the requested revision/task scope')
        grouped[episode['revision']].append(id)
    validation, originals = [], {}
    if 'evaluation' in outputs:
        for revision, (source, build, world, selected) in environments.items():
            rows = validation_cases(root, project, source, build, world, grouped[revision], required=required)
            if not rows or not all(row['passed'] for row in rows):
                raise ValueError('environment lacks applicable generated validation cases matching frozen expectations')
            validation.extend({**row, 'revision':revision} for row in rows)
            for id in selected:
                originals[(revision,id)] = _case_interface_source(root,project,source,build,world,id,grouped[revision],required=required)
    if result is None:
        directory.mkdir(parents=True, exist_ok=False)
    else:
        if result.get('artifactRef') != str((directory/'report.json').relative_to(root)):
            raise ValueError('consumer report leaves its fixed request scope')
        saved = json.loads((directory/'report.json').read_text())
        if not _same_json(saved, {key:value for key,value in result.items() if key != 'artifactRef'}):
            raise ValueError('consumer report differs from saved result')
    report = {'schema':'sceneagent-product-consumption-v1', 'projectId':payload['projectId'],
        'revision':payload.get('revision', refs[0]['revision']), 'requiredOutputs':outputs, 'environmentRefs':refs,
        'validationEpisodes':ids,
        'limitations':['技术消费不证明任务语义、物理重演或策略稳健；诊断评分不改变原记录结果。']}
    if 'demonstrations' in outputs:
        report['demonstrations']={'passed':True,'episodeCount':len(demonstrations),'batches':demonstrations}
    if 'evaluation' in outputs:
        private=directory/'host-private-score.json'
        if result is None: save(private, {'scoreSecret':'host-only'})
        required.add(private)
        interfaces, checks, reloads = [], {}, []
        supplied = result.get('evaluation', {}).get('interfaces') if result is not None else None
        if result is not None and (not isinstance(supplied,list) or len(supplied) != len(originals)
                or {(row.get('revision'),row.get('taskId')) for row in supplied} != set(originals)):
            raise ValueError('consumer interface revision/task coverage is incomplete or duplicated')
        for revision, (source, build, world, _) in environments.items():
            _, env_directory, env_manifest, env_world = _consumed_environment(root, {**payload,'revision':revision}, build)
            reloads.append({'revision':revision,'passed':True,'newProcess':True,'nodes':len(world.nodes),
                'frozenDependenciesChecked':len(build['assets']),'originalProjectDirectoryRequired':False})
            probes=[('outputReadBlocked',str(private),False),('outputWriteBlocked',str(private),True),
                ('frozenSourceWriteBlocked',str(env_directory/'scene.py'),True),('runtimeWriteBlocked',str(Path(__file__)),True),
                ('parentMemoryBlocked',f'/proc/{os.getpid()}/mem',False),('parentDescriptorsBlocked',f'/proc/{os.getpid()}/fd/0',False),
                ('undeclaredSceneReadBlocked',str(env_directory/'scene.json'),False)]
            for (rev,id), (original,batch) in originals.items():
                if rev != revision: continue
                if result is None:
                    location=directory/'interfaces'/revision/id;location.mkdir(parents=True)
                    selected=_interface_source(batch['transitions'][0]['executedAction'],build['io'])
                    (location/selected['sourceRef']).write_text(selected['sourceFiles'][selected['sourceRef']])
                    task=next(Task(**row) for row in build['tasks'] if row['id']==id)
                    probe=_consumer_interface_episode(root,project,env_directory,env_manifest,env_world,task,selected,'candidate',
                        {**payload,'revision':revision,'purpose':'consumer_diagnostic'},output_parent=location/'episodes',interface_probes=probes)
                    episode_ref=probe['artifacts']['manifest']
                else:
                    episode_ref=next(row['episodeRef'] for row in supplied if (row['revision'],row['taskId'])==(revision,id))
                row, checked, isolated=_interface_evidence(root,env_directory,env_manifest,env_world,directory,
                    original,batch,episode_ref,revision_scope=True)
                interfaces.append(row);required.update(checked)
                checks.update({revision+':'+id+':'+key:value for key,value in isolated.items()})
        isolation={'passed':bool(checks) and all(checks.values()),'checks':checks,
            'mechanism':'bubblewrap_unshare_all_explicit_dependency_mounts'}
        if result is None:save(directory/'isolation.json',isolation)
        elif json.loads((directory/'isolation.json').read_text()) != isolation:
            raise ValueError('consumer isolation differs from actual evidence')
        required.add(directory/'isolation.json')
        report['reload']={'passed':True,'newProcess':True,'environments':reloads,'originalProjectDirectoryRequired':False}
        if payload.get('environmentRoot') is not None:report['reload']['environmentRoot']=payload['environmentRoot']
        report['evaluation']={'passed':isolation['passed'] and all(row['passed'] for row in interfaces),
            'tasks':validation,'episodeCount':len(ids),'isolation':isolation,'interfaces':interfaces}
    report['passed']=all(report[key]['passed'] for key in ('demonstrations','reload','evaluation') if key in report)
    required.add(directory/'report.json')
    if result is None:
        save(directory/'report.json',report)
        return {**report,'artifactRef':str((directory/'report.json').relative_to(root))}
    if not _same_json(report,{key:value for key,value in result.items() if key != 'artifactRef'}):
        raise ValueError('consumer selected products differ from their independently verified evidence')
    return required


@measure('consume')
def consume_internal(payload, root):
    from runtime import consumption
    if consumption.needs_dispatch(payload, root):
        return consumption.consume(payload, root)
    if ('requiredOutputs' in payload or 'environmentRefs' in payload
            or any(json.loads((root/'projects'/payload['projectId']/'episodes'/row['episodeId']/'manifest.json').read_text()).get('robotSetupRef')
                   for row in _consume_selection(payload))):
        return _consume_scoped(payload, root)
    selections = _consume_selection(payload)
    project, directory, manifest, world = frozen(root, payload)
    validation = validation_cases(root, project, directory, manifest, world, payload.get('validationEpisodes', []))
    if not validation or not all(row['passed'] for row in validation):
        raise ValueError('environment lacks actual applicable generated validation cases matching frozen expectations')
    output = project/'consumer-reports'/valid_id(payload.get('requestId') or uuid.uuid4().hex[:16])
    output.mkdir(parents=True, exist_ok=False)
    private = output/'host-private-score.json'; save(private, {'scoreSecret': 'host-only'})
    _, environment_directory, environment_manifest, environment_world = (
        _consumed_environment(root, payload, manifest) if payload.get('environmentRoot') is not None
        else (project, directory, manifest, world))
    probes = [('outputReadBlocked', str(private), False), ('outputWriteBlocked', str(private), True),
              ('frozenSourceWriteBlocked', str(environment_directory/'scene.py'), True), ('runtimeWriteBlocked', str(Path(__file__)), True),
              ('parentMemoryBlocked', f'/proc/{os.getpid()}/mem', False), ('parentDescriptorsBlocked', f'/proc/{os.getpid()}/fd/0', False),
              ('undeclaredSceneReadBlocked', str(environment_directory/'scene.json'), False)]
    demonstrations, originals, covered = [], {}, set()
    for selection in selections:
        summary, batch, _ = _behavior_data(root, project, directory, manifest, world, selection)
        demonstrations.append(summary); originals.setdefault(selection['taskId'], (summary, batch)); covered.add(selection['taskId'])
    if covered != {row['id'] for row in manifest['tasks']}:
        raise ValueError('required tasks lack verified behavior data')
    interfaces, isolation = [], {}
    for task_id, (source, batch) in originals.items():
        task = next(Task(**row) for row in manifest['tasks'] if row['id'] == task_id)
        location = output/'interfaces'/task_id; location.mkdir(parents=True)
        selected = _interface_source(batch['transitions'][0]['executedAction'], manifest['io'])
        save_source = location/selected['sourceRef']; save_source.write_text(selected['sourceFiles'][selected['sourceRef']])
        flush_performance('consume:interface', progress={'taskId': task_id})
        probe = _consumer_interface_episode(root, project, environment_directory, environment_manifest, environment_world, task, selected, 'candidate',
            {**payload, 'purpose': 'consumer_diagnostic'}, output_parent=location/'episodes', interface_probes=probes)
        row, _, checks = _interface_evidence(root, environment_directory, environment_manifest, environment_world, output, source, batch, probe['artifacts']['manifest'])
        interfaces.append(row); isolation.update({task_id+':'+name: value for name, value in checks.items()})
    isolation = {'passed': bool(isolation) and all(isolation.values()), 'checks': isolation,
                 'mechanism': 'bubblewrap_unshare_all_explicit_dependency_mounts'}
    save(output/'isolation.json', isolation)
    report = {'schema': 'sceneagent-product-consumption-v1', 'projectId': payload['projectId'], 'revision': manifest['revision'],
        'reload': {'passed': True, 'newProcess': True, 'nodes': len(world.nodes), 'frozenDependenciesChecked': len(manifest['assets']),
                   'originalProjectDirectoryRequired': False},
        'demonstrations': {'passed': bool(demonstrations), 'episodeCount': len(demonstrations), 'batches': demonstrations},
        'evaluation': {'passed': all(row['passed'] for row in validation) and isolation['passed'] and all(row['passed'] for row in interfaces),
            'tasks': validation, 'episodeCount': len(payload.get('validationEpisodes', [])), 'isolation': isolation, 'interfaces': interfaces},
        'validationEpisodes': payload.get('validationEpisodes', []),
        'limitations': ['技术消费不证明任务语义、物理重演或策略稳健；诊断评分不改变原记录结果。']}
    if payload.get('environmentRoot') is not None:
        report['reload']['environmentRoot'] = payload['environmentRoot']
    report['passed'] = report['reload']['passed'] and report['demonstrations']['passed'] and report['evaluation']['passed']
    save(output/'report.json', report); flush_performance('consume:report_written', report)
    return {**report, 'artifactRef': str((output/'report.json').relative_to(root))}


def _observed_files(payload, result, root, project, source_directory, build, world):
    camera_id, scope = payload.get('cameraId'), payload.get('nodeId') or payload.get('nodeScope')
    if payload.get('episode'):
        if scope is not None:
            raise ValueError('recorded episode frames cannot be reframed by node')
        episode_directory = project/'episodes'/valid_id(payload['episode'])
        episode = json.loads((episode_directory/'manifest.json').read_text())
        validate_publication(episode, root)
        source_directory, build, world, _ = robot_environment(root, project, source_directory, build, world, episode)
        task = next((Task(**row) for row in build['tasks'] if row['id'] == episode.get('taskId')), None)
        if task is None or episode.get('revision') != build['revision'] or episode.get('recording') != task.recording:
            raise ValueError('observed episode camera differs from the frozen task')
        trajectory = json.loads((episode_directory/'trajectory.json').read_text())
        frame, camera = _episode_frame(episode, trajectory, camera_id, payload.get('frame'))
        selected_camera = build['compiled'].get('recordingCameras', {}).get(task.id, {}).get(frame['cameraId'])
        declaration = next(row for row in task.recording['cameras'] if row['cameraId'] == frame['cameraId'])
        if (selected_camera is None or frame.get('cameraScope') != selected_camera['scope']
                or frame.get('cameraPrimPath') != selected_camera['primPath']
                or frame.get('intrinsics') != selected_camera['intrinsics']
                or frame.get('coordinateSpec') != selected_camera['coordinateSpec']
                or result.get('checkedScope') != 'recorded_camera_frame'
                or any(result.get('camera', {}).get(key) != value for key, value in declaration.items())):
            raise ValueError('observed episode camera identity differs from its frozen task selection')
        path = within(episode_directory, frame['path'])
        if result.get('artifactRef') != str(path.relative_to(root)) or digest(path) != frame['sha256']:
            raise ValueError('observed pixels differ from the requested episode frame')
        if result.get('cameraId') != frame['cameraId'] or result.get('globalFrameIndex') != frame['index'] or result.get('frameIndex') != frame['frameIndex']:
            raise ValueError('observed frame identity differs from the selected camera')
        width, height = camera['resolution']
    else:
        stage = None
        if scope is not None and world.native:
            from pxr import Usd
            stage = Usd.Stage.Open(str(source_directory/build['compiled']['usd']))
        _, camera, camera_id = _observation_camera(build, world, scope, camera_id, stage=stage)
        path = within(root, result['artifactRef'])
        if not path.is_relative_to(source_directory/'observations'):
            raise ValueError('observation leaves the frozen scene output scope')
        frame = json.loads(path.with_suffix('.camera.json').read_text())
        width, height = camera.get('resolution', [camera.get('width', 1280), camera.get('height', 720)])
        expected_id = camera_id if camera_id is not None else camera.get('cameraId', 'preview')
        if (frame.get('cameraId') != expected_id or frame.get('cameraScope') != camera['scope']
                or frame.get('cameraPrimPath') != camera.get('primPath', '/World/camera')
                or frame.get('sha256') != digest(path)):
            raise ValueError('observation frame differs from its frozen camera identity or pixels')
        expected_intrinsics = camera.get('intrinsics', {'fx': width*24/36, 'fy': width*24/36, 'cx': width/2, 'cy': height/2})
        if frame.get('intrinsics') != expected_intrinsics:
            raise ValueError('observation camera calibration differs from the frozen lens')
        if (result.get('checkedScope') != 'rendered_camera_frame' or result.get('requestedScope') != scope
                or result.get('nodeScope') != scope
                or any(result.get(key) != frame.get(field) for key, field in (
                    ('cameraId', 'cameraId'), ('cameraScope', 'cameraScope'), ('cameraPrimPath', 'cameraPrimPath'),
                    ('globalFrameIndex', 'index'), ('frameIndex', 'frameIndex')))):
            raise ValueError('observation camera or scope differs from its actual selected frame')
        actual_camera = result.get('camera', {})
        import numpy as np
        from runtime.motion import rigid
        if (camera.get('frame', 'world') == 'world' and 'worldFromCamera' in camera
                and not np.allclose(rigid(frame.get('worldFromCamera')), rigid(camera['worldFromCamera']), atol=1e-6, rtol=0)):
            raise ValueError('observed world camera pose differs from its frozen source beyond SDK precision')
        if (not isinstance(actual_camera, dict)
                or any(actual_camera.get(key) != value for key, value in camera.items() if key != 'worldFromCamera')
                or any(actual_camera.get(key) != frame.get(key) for key in ('intrinsics', 'coordinateSpec'))
                or not np.allclose(rigid(actual_camera.get('worldFromCamera')), rigid(frame.get('worldFromCamera')), atol=1e-12, rtol=0)):
            raise ValueError('observation camera source or calibration differs from its frozen selection')
    import imageio.v2 as imageio
    if imageio.imread(path).shape != (height, width, 3):
        raise ValueError('observed image differs from its actual camera resolution')
    if (result.get('cameraScope') != frame.get('cameraScope') or result.get('cameraPrimPath') != camera.get('primPath', '/World/camera')
            or result.get('image', {}).get('mimeType') != 'image/png'
            or base64.b64decode(result['image']['data'], validate=True) != path.read_bytes()):
        raise ValueError('observation camera identity or returned pixels differ from the actual frame')
    return {path} if payload.get('episode') else {path, path.with_suffix('.camera.json')}


def required_files(payload, result):
    """Parent-owned minimums plus all frozen dependencies; child cannot shorten these."""
    root = Path(payload['root']).resolve()
    operation = payload['operation']
    project = root / 'projects' / valid_id(payload['projectId']) if payload.get('projectId') else None
    if operation != 'prepare' and result.get('projectId') is not None and result['projectId'] != payload.get('projectId'):
        raise ValueError('worker result project identity mismatch')
    if payload.get('revision') and result.get('revision') != payload['revision']:
        raise ValueError('worker result scene revision mismatch')
    if operation == 'build':
        validate_protocol(result, 'sceneagent-build-v6')
        directory = project/'revisions'/valid_id(result['revision'])
        expected_sources = source_files(payload)
        if set(result['sourceDigests']) != set(expected_sources):
            raise ValueError('frozen source set differs from the request')
        for name, source in expected_sources.items():
            if within(directory, name).read_text() != source:
                raise ValueError('frozen source differs from the request')
        frozen_names = {'scene.json', 'tasks.json', 'scene-source.usda', 'native-stage-audit.json'}
        preview = result.get('artifacts', {}).get('preview')
        if preview is not None:
            if preview != str((directory/'preview.glb').relative_to(root)):
                raise ValueError('web preview must reference the current frozen version')
            frozen_names.add('preview.glb')
        if set(result['frozenArtifactDigests']) != frozen_names:
            raise ValueError('build frozen artifact digest set is incomplete')
        required = {directory/'manifest.json', *(directory/name for name in frozen_names)}
        required.update(within(directory, name) for name in result['assets'])
        required.update(within(directory, name) for name in expected_sources)
        saved_diagnostics = json.loads((directory/'manifest.json').read_text()).get('diagnostics')
        if 'diagnostics' in result or saved_diagnostics is not None:
            diagnostics = result.get('diagnostics')
            if (diagnostics != saved_diagnostics or not isinstance(diagnostics, dict)
                    or 'nativeBuildLog' not in diagnostics or set(diagnostics)-set(NATIVE_DIAGNOSTICS)):
                raise ValueError('build diagnostic log identity or digest differs')
            for label, log in diagnostics.items():
                log_path = directory/NATIVE_DIAGNOSTICS[label]
                if (not isinstance(log, dict) or set(log) != {'artifactRef','sha256'}
                        or log['artifactRef'] != str(log_path.relative_to(root)) or digest(log_path) != log['sha256']):
                    raise ValueError('build diagnostic log identity or digest differs')
                required.add(log_path)
        required.update(verify_inputs(result, runtime_key(), directory))
        if result.get('episodeLimits') != {'tasks': {row['id']: Task(**row).execution for row in result['tasks']}, 'reset': 'frozen_seed_only'}:
            raise ValueError('build episode limits differ from the frozen task declarations')
        if json.loads((directory/'tasks.json').read_text()) != result['tasks']:
            raise ValueError('build task snapshot differs from its manifest')
        world = World.restore(json.loads((directory/'scene.json').read_text()))
        if not world.native or not result['tasks']:
            raise ValueError('build requires native semantics and generated tasks')
        for definition in result['tasks']:
            task = Task(**definition)
            task.bind(world)
            rubric_sources(task, expected_sources, result['sourceDigests'])
            evaluation_sources(task, expected_sources, result['sourceDigests'])
        if set(result.get('resources', {})) != set(payload.get('resourceRefs', [])):
            raise ValueError('native resources differ from the declared preparation references')
        for reference, entry in result.get('resources', {}).items():
            original = within(root, reference)
            report = json.loads(original.read_text())
            expected_entry = 'dependencies/resources/'+digest(original)+'/'+report['entry']
            if entry != expected_entry or not within(directory, entry).is_file():
                raise ValueError('native resource entry differs from its frozen prepared bundle')
        return required
    if operation in ('rollout', 'evaluate'):
        episodes = result.get('episodes', [])
        if not episodes or len({row['episode'] for row in episodes}) != len(episodes):
            raise ValueError('execution lacks unique episode receipts')
        _, build_directory, build, world = frozen(root, {**payload, 'revision': result['revision']})
        build_directory, build, world, setup_files = robot_environment(root, project, build_directory, build, world, payload)
        files = frozen_sources(build_directory, build)
        required = set(setup_files)
        candidate_source = None
        if operation == 'evaluate':
            if len(episodes) != 1 or episodes[0]['taskId'] != payload['taskId'] or result.get('taskId') != payload['taskId']:
                raise ValueError('candidate evaluation task set differs from the external request')
            if payload.get('requestId') and result.get('evaluationId') != payload['requestId']:
                raise ValueError('candidate evaluation scope differs from the request identity')
            candidate = payload['candidate']
            candidate_source = source_closure({key: candidate[key] for key in ('sourceRef', 'helperRefs')},
                                              candidate['sourceFiles'], candidate['sourceDigests'])
            evaluation_root = project/'evaluations'/valid_id(result['evaluationId'])
            required.add(evaluation_root/'report.json')
            for name, source in candidate_source['sourceFiles'].items():
                path = within(evaluation_root/'candidate', name)
                if path.read_text() != source:
                    raise ValueError('candidate source snapshot differs from the external request')
                required.add(path)
            if result.get('candidate') != {key: candidate_source[key] for key in ('sourceRef', 'sourceDigest', 'helperDigests')}:
                raise ValueError('candidate descriptor differs from its independent source lock')
        else:
            requested = payload.get('taskIds') or ([payload['taskId']] if payload.get('taskId') else None)
            requested = requested or [build['tasks'][0]['id']]
            repetitions = len(payload.get('seeds') or [build['seed']]) * payload.get('repetitions', 1)
            if ({row['taskId'] for row in episodes} != set(requested)
                    or any(sum(row['taskId'] == id for row in episodes) != repetitions for id in requested)):
                raise ValueError('rollout task coverage differs from request')
        for row in episodes:
            if row['projectId'] != payload['projectId'] or row['revision'] != build['revision']:
                raise ValueError('episode scope differs from the request')
            task = next((Task(**task) for task in build['tasks'] if task['id'] == row['taskId']), None)
            if task is None:
                raise ValueError('unknown episode task')
            if operation == 'evaluate':
                selected, kind, case = candidate_source, 'candidate', None
                episode_directory = evaluation_root/'episodes'/valid_id(row['episode'])
            else:
                kind, selected, case = _select_controller(task, files, build, payload)
                episode_directory = project/'episodes'/valid_id(row['episode'])
                if 'behavior' in payload:
                    reference, behavior_files = behavior_snapshot(root, project, selected)
                    required.update(behavior_files)
                    if row.get('behaviorRef') != reference:
                        raise ValueError('episode behavior reference differs from the requested source')
            report, checked = verify_episode(root, episode_directory, build_directory, build, world, selected=selected)
            episode = json.loads((episode_directory/'manifest.json').read_text())
            if episode.get('behaviorRef') != row.get('behaviorRef'):
                raise ValueError('episode behavior reference differs from its receipt')
            expected_setup = build.get('_robotSetup', {})
            for value in (result, row, episode):
                if any(value.get(key) != expected_setup.get(key) for key in ('robotSetupRef','robotSetupSha256')):
                    raise ValueError('execution robot installation differs from the requested input')
            if expected_setup and row.get('recording') != task.recording:
                raise ValueError('execution recording differs from its installed task views')
            receipts = (result, row, episode, report)
            if ('workerTimeoutSeconds' in payload
                    or build.get('runtimeExecution', {}).get('workerTimeoutParameter') == 'workerTimeoutSeconds'
                    or any('workerTimeoutSeconds' in value for value in receipts)):
                if any('workerTimeoutSeconds' not in value or worker_timeout(value) != worker_timeout(payload) for value in receipts):
                    raise ValueError('execution receipt timeout budget differs from the actual request')
            if not _same_json(row['score'], report):
                raise ValueError('episode result differs from the saved parent-verified report')
            if row.get('executionKind') != episode['executionKind']:
                raise ValueError('episode actuator execution kind differs from its frozen I/O')
            if episode.get('limits') != episode_budgets(payload, task) or episode.get('seed') != build['seed']:
                raise ValueError('episode reset or budgets differ from the frozen request')
            if episode['controllerKind'] != kind or episode['purpose'] != ('evaluation' if operation == 'evaluate' else payload.get('purpose', 'demonstration')):
                raise ValueError('episode controller role or purpose differs from request')
            if case is not None and (episode.get('caseId') != case['id'] or episode.get('expected') != case['expected']):
                raise ValueError('episode validation identity differs from its frozen generated case')
            eligible = bool(kind == 'expert' and episode['purpose'] == 'demonstration'
                and report['executionStatus'] == 'completed' and report['scoreStatus'] == 'available'
                and report['success'] is True and episode.get('control', {}).get('isolated') is True
                and has_control_ports(build['io']))
            if bool(episode.get('candidateDemonstrationEligible', episode.get('demonstrationEligible'))) != eligible or bool(row.get('demonstrationEligible')) != eligible:
                raise ValueError('episode demonstration eligibility differs from verified execution')
            if candidate_source is not None and eligible:
                raise ValueError('external candidate cannot be promoted to a generated demonstration')
            required.update(checked)
        if candidate_source is not None:
            summary_path = evaluation_root/'report.json'
            if result.get('artifactRef') != str(summary_path.relative_to(root)):
                raise ValueError('candidate report artifact scope differs from its evaluation')
            if result.get('demonstrationEligible') is not False:
                raise ValueError('candidate demonstration eligibility must remain false')
            for field, expected in (('result', report['result']), ('executionStatus', report['executionStatus']),
                                    ('scoreStatus', report['scoreStatus'])):
                if not _same_json(result.get(field), expected):
                    raise ValueError('candidate summary '+field+' differs from its parent-verified episode')
            if not _same_json(json.loads(summary_path.read_text()), result):
                raise ValueError('candidate saved report differs from the returned summary')
        return required
    if operation == 'observe':
        if payload.get('output') == 'web_preview':
            return _web_preview_files(payload, result, root)
        project, source_directory, build, world = frozen(root, payload)
        camera_ids = _observation_ids(payload)
        if camera_ids is None:
            return _observed_files(payload, result, root, project, source_directory, build, world)
        observations = result.get('observations')
        if (result.get('cameraIds') != camera_ids or not isinstance(observations, list)
                or len(observations) != len(camera_ids)):
            raise ValueError('batch observation selection differs from the request')
        required, frames = set(), []
        for name, row in zip(camera_ids, observations):
            if not isinstance(row, dict) or row.get('cameraId') != name or row.get('revision') != build['revision']:
                raise ValueError('batch observation camera or revision differs from its request')
            required.update(_observed_files({**payload, 'cameraId': name}, row, root, project, source_directory, build, world))
            frames.append(json.loads(within(root, row['artifactRef']).with_suffix('.camera.json').read_text()))
        _observation_batch_clock(frames)
        return required
    if operation == 'prepare':
        report_path = Path(result['reportPath']).resolve()
        if not report_path.is_relative_to(root):
            raise ValueError('prepared report outside configured data root')
        report = json.loads(report_path.read_text())
        required = {report_path, report_path.parent / 'source.json'}
        if report['kind'] == 'material':
            required.add(report_path.parent / 'baseColor.png')
            required.update(within(report_path.parent, name) for name in report['maps'].values())
        elif report['kind'] == 'static_mesh':
            required.add(report_path.parent / 'visual.glb')
        elif report['kind'] == 'native_files':
            if (report['entry'] not in report['nativeFiles']
                    or not set(report['nativeFiles']).issubset(report['frozenDigests'])):
                raise ValueError('native prepared entry or declared file set mismatch')
            required.add(within(report_path.parent, report['entry']))
        else:
            raise ValueError('unknown prepared asset kind')
        if set(report['preparedFiles']) != {'report.json', *report['frozenDigests']}:
            raise ValueError('prepared file digest set mismatch')
        for name, expected in report['frozenDigests'].items():
            if digest(within(report_path.parent, name)) != expected:
                raise ValueError('prepared source or texture digest mismatch')
        required.update(within(report_path.parent, name) for name in report['preparedFiles'])
        if result.get('artifactRef'):
            required.add(within(root, result['artifactRef']))
        if result.get('thumbnailRef'):
            required.add(within(root, result['thumbnailRef']))
        return required
    if operation == 'consume':
        from runtime import consumption
        if consumption.needs_dispatch(payload, root):
            return consumption.consume(payload, root, result)
        if ('requiredOutputs' in payload or 'environmentRefs' in payload
                or any(json.loads((root/'projects'/payload['projectId']/'episodes'/row['episodeId']/'manifest.json').read_text()).get('robotSetupRef')
                       for row in _consume_selection(payload))):
            return _consume_scoped(payload, root, result)
        if result.get('projectId') != payload['projectId'] or result.get('revision') != payload.get('revision'):
            raise ValueError('consumer result differs from its requested project/revision')
        if result.get('schema') != 'sceneagent-product-consumption-v1':
            raise ValueError('consumer requires the current product-consumption report schema')
        project, source_directory, manifest, world = frozen(root, {**payload, 'revision': result['revision']})
        selections = _consume_selection(payload)
        directory = within(root, result['artifactRef']).parent
        expected_directory = project/'consumer-reports'/valid_id(payload['requestId'])
        if directory != expected_directory or Path(result['artifactRef']).name != 'report.json':
            raise ValueError('consumer report leaves its fixed request scope')
        saved = json.loads((directory/'report.json').read_text())
        if not _same_json(saved, {key: value for key, value in result.items() if key != 'artifactRef'}):
            raise ValueError('consumer report differs from saved result')
        ids = payload.get('validationEpisodes', [])
        if result.get('validationEpisodes') != ids:
            raise ValueError('consumer validation episode set differs from request')
        required = {directory/'report.json', directory/'host-private-score.json', directory/'isolation.json'}
        validation = validation_cases(root, project, source_directory, manifest, world, ids, required=required)
        if not _same_json(result['evaluation']['tasks'], validation) or not all(row['passed'] for row in validation):
            raise ValueError('consumer validation differs from actual frozen cases')
        originals, batches = {}, []
        for selection in selections:
            summary, batch, checked = _behavior_data(root, project, source_directory, manifest, world, selection)
            batches.append(summary); originals.setdefault(selection['taskId'], (summary, batch)); required.update(checked)
        if (not _same_json(result['demonstrations'], {'passed': bool(batches), 'episodeCount': len(batches), 'batches': batches})
                or set(originals) != {row['id'] for row in manifest['tasks']}):
            raise ValueError('consumer behavior batch/selection/use differs from its actual records')
        interfaces = result['evaluation'].get('interfaces')
        if (not isinstance(interfaces, list) or len(interfaces) != len(originals)
                or {row.get('taskId') for row in interfaces} != set(originals)):
            raise ValueError('consumer interface task coverage is incomplete or duplicated')
        _, environment_directory, environment_manifest, environment_world = (
            _consumed_environment(root, payload, manifest) if payload.get('environmentRoot') is not None
            else (project, source_directory, manifest, world))
        checks = {}
        for row in interfaces:
            source, batch = originals[row['taskId']]
            expected, checked, isolated = _interface_evidence(root, environment_directory, environment_manifest, environment_world, directory,
                source, batch, row['episodeRef'])
            if not _same_json(expected, row):
                raise ValueError('consumer interface evidence differs from actual source, records or clock')
            required.update(checked)
            checks.update({row['taskId']+':'+name: value for name, value in isolated.items()})
        isolation = {'passed': bool(checks) and all(checks.values()), 'checks': checks,
                     'mechanism': 'bubblewrap_unshare_all_explicit_dependency_mounts'}
        if (json.loads((directory/'isolation.json').read_text()) != isolation
                or result['evaluation']['isolation'] != isolation
                or result['evaluation']['episodeCount'] != len(ids)
                or result['evaluation']['passed'] is not True):
            raise ValueError('consumer interface/isolation evaluation summary differs from its actual evidence')
        reload = {'passed': True, 'newProcess': True, 'nodes': len(world.nodes),
            'frozenDependenciesChecked': len(manifest['assets']), 'originalProjectDirectoryRequired': False}
        if payload.get('environmentRoot') is not None:
            reload['environmentRoot'] = payload['environmentRoot']
        if result.get('passed') is not True or result.get('reload') != reload:
            raise ValueError('consumer summary differs from verified evidence')
        return required
    raise ValueError('operation has no supervised artifact contract')


def finalize(payload, result):
    root = Path(payload['root']).resolve()
    project = root / 'projects' / valid_id(payload['projectId']) if payload.get('projectId') else None
    if payload['operation'] == 'build':
        path = project / 'revisions' / valid_id(result['revision']) / 'manifest.json'
        manifest = json.loads(path.read_text())
        if manifest['status'] == 'candidate':
            manifest.update(status='completed', publicationJobId=valid_id(payload['jobId']))
            save(path, manifest)
        else:
            validate_publication(manifest, root)
        save(project / 'latest.json', manifest)
        return {**manifest, 'publicationArtifactDigests': {str(path.relative_to(root)): digest(path)}}
    if payload['operation'] in ('rollout', 'evaluate'):
        published = {}
        for row in result['episodes']:
            path = within(root, row['artifacts']['manifest'])
            manifest = json.loads(path.read_text())
            if 'behaviorRef' in manifest:
                selected, _ = recorded_behavior(root, project, manifest)
                if any(manifest.get(field) != selected[key] for field, key in (
                        ('controllerSource','sourceRef'),('controllerDigest','sourceDigest'),('controllerHelperDigests','helperDigests'))):
                    raise ValueError('published expert differs from its independent behavior source')
            eligible = bool(manifest.pop('candidateDemonstrationEligible', False)
                            and row['score']['success'] is True and manifest['control']['isolated']
                            and manifest['purpose'] == 'demonstration' and has_control_ports(manifest['io'])
                            and manifest['controllerKind'] == 'expert'
                            and (manifest['controllerSource'] == 'expert.py' or 'behaviorRef' in manifest))
            manifest.update(status='completed' if not row['score'].get('error') else 'failed',
                            publicationJobId=valid_id(payload['jobId']), demonstrationEligible=eligible)
            save(path, manifest)
            row['demonstrationEligible'] = eligible
            published[str(path.relative_to(root))] = digest(path)
            if manifest.get('robotSetupRef'):
                installed = within(root, manifest['robotSetupRef'])
                if digest(installed) != manifest['robotSetupSha256']:
                    raise ValueError('robot installation changed before publication')
                published[manifest['robotSetupRef']] = manifest['robotSetupSha256']
        result['publicationArtifactDigests'] = published
        result['demonstrationEligible'] = result['episodes'][0]['demonstrationEligible']
        if payload['operation'] == 'rollout':
            save(project / 'latest_episode.json', result['episodes'][-1])
        else:
            save(project/'evaluations'/valid_id(result['evaluationId'])/'report.json', result)
    return result


def material_preflight(payload):
    """Select frozen material JSON before Kit without importing pxr or restoring a stage."""
    if not payload.get('root') or not payload.get('projectId'):
        return  # Unit entry-order fixtures have no project inputs.
    root = Path(payload['root']).resolve()
    project = root/'projects'/valid_id(payload['projectId'])
    revision = payload.get('revision') or json.loads((project/'latest.json').read_text())['revision']
    directory = within(project/'revisions', revision)
    manifest = json.loads((directory/'manifest.json').read_text())
    if payload.get('robotSetupRef'):
        path = within(root, payload['robotSetupRef'])
        if path.parent.parent != project/'robot-setups' or path.name != 'manifest.json':
            raise ValueError('MDL installation preflight leaves this project')
        if payload.get('robotSetupSha256') and digest(path) != payload['robotSetupSha256']:
            raise ValueError('MDL installation manifest changed')
        manifest = json.loads(path.read_text())['environment']
        directory = path.parent
    name = manifest['compiled']['nativeStageAudit']
    path = within(directory, name)
    if manifest['frozenArtifactDigests'].get(name) != digest(path):
        raise ValueError('MDL native audit is not frozen before SDK startup')
    audit = json.loads(path.read_text())
    if audit.get('mdl') is not None:
        locked = {**manifest['assets'], **manifest['frozenArtifactDigests']}
        for relative, checksum in audit['files'].items():
            if locked.get(relative) != checksum or digest(within(directory, relative)) != checksum:
                raise ValueError('MDL preflight file closure changed')
        from runtime.simulation import configure_native_materials
        configure_native_materials(directory, audit)


def kit_entry(directory):
    global JOB_DIRECTORY
    from runtime.jobs import write_candidate, _token, performance_context
    parent = json.loads((directory / 'worker-parent.json').read_text())
    if sys.platform == 'linux':
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(1, 15, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'cannot bind worker lifetime to supervisor')
    if os.getppid() != parent['pid'] or _token(parent['pid']) != parent['processToken']:
        raise RuntimeError('Isaac worker supervisor ended before initialization')
    from runtime import simulation
    if (directory / 'status.json').exists():
        JOB_DIRECTORY = directory
    payload = json.loads((directory / 'worker-request.json').read_text())
    with performance_context(directory, payload):
        code = 7
        try:
            from runtime.consumption import needs_dispatch
            if ((payload['operation'] in ('rollout', 'consume', 'evaluate') or (payload['operation'] == 'observe' and not payload.get('episode')))
                    and not payload.get('robotSetup') and not needs_dispatch(payload, Path(payload.get('root', '.')).resolve())
                    and not consume_uses_mdl(payload)):
                flush_performance('sdk_initialization')
                material_preflight(payload)
                simulation._ensure_app()
            flush_performance('run')
            result = run(payload, child=True)
            flush_performance('finish', result)
            termination = simulation.finish()
            flush_performance('required_files', result)
            with measure('requiredFiles'):
                files = required_files(payload, result)
            flush_performance('candidate_write', result)
            with measure('candidateWrite'):
                write_candidate(directory, payload, result, files,
                                {'records': True, 'images': True, 'encoder': True, 'replicator': True}, termination)
            code = 0
            flush_performance('business_complete', result)
        except BaseException as exc:
            failure = {'error': f'{type(exc).__name__}: {exc}', 'traceback': traceback.format_exc(), 'businessExitCode': code}
            try:
                save(directory / 'worker-failure.json', failure)
            except (OSError, ValueError, TypeError, OverflowError, RecursionError) as diagnostic:
                exc.add_note('worker failure diagnostic could not be written: '+repr(diagnostic))
                try:
                    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
                except (OSError, ValueError):
                    pass
            flush_performance('business_failed')
        finally:
            simulation.shutdown(code)
        sys.exit(code)


def consume(payload, root):
    return run({**payload, 'root': str(root), 'operation': 'consume'})


def evaluate(payload, root):
    return run({**payload, 'root': str(root), 'operation': 'evaluate'})


def run(payload, child=False):
    root = Path(payload['root']).resolve()
    legacy = Path(payload.get('legacyRoot') or root/'asset-library').resolve()
    operation = payload['operation']
    from runtime.motion import motion_runtime_identity
    motion_runtime_identity.cache_clear()  # One fixed SDK identity per operation.
    if operation == 'health':
        from runtime.robot import robot_capabilities
        capabilities = robot_capabilities(None, runtime_key())
        return {'ready': True, 'engine': 'isaacsim', 'physics': 'physx', 'runtimeVersions': runtime_versions(), 'dataRoot': str(root),
                'robotReady': False, 'capabilities': capabilities,
                'legacyAvailable': legacy.exists(), 'expertSkills': capabilities['availableApis'],
                'formalIsolationAvailable': bool(shutil.which('bwrap'))}
    if operation == 'submit':
        from runtime.jobs import submit
        return submit(Path(__file__).resolve().parent, root, payload)
    if operation == 'job':
        from runtime.jobs import inspect
        return inspect(root, payload['jobId'], payload.get('action', 'status'),
                       int(payload.get('cursor', 0)), payload.get('waitSeconds', 30))
    if operation == 'assets':
        filters = {key: payload[key] for key in ('kind', 'category', 'textured', 'size', 'libraryTier', 'collection', 'requiresJoints') if key in payload}
        result = assets(legacy, payload.get('query', ''), int(payload.get('limit', 24)), root, payload.get('assetId'), **filters)
        if payload.get('assetId'):
            for row in result['items']:
                if row.get('packageRoot'):
                    row.update(publisher_preview(row, Path(row['packageRoot']), root))
        return result
    if operation == 'prepare' and sum(key in payload for key in ('assetId', 'url', 'sourceFile', 'resourceFile')) != 1:
        raise ValueError('asset preparation requires exactly one assetId/url/sourceFile')
    if not child and operation in ('prepare', 'build', 'rollout', 'observe', 'consume', 'evaluate'):
        from runtime.jobs import supervise, submit, inspect
        if JOB_DIRECTORY is None:
            request = {**payload, 'requestId': payload.get('requestId') or ('request_' + uuid.uuid4().hex),
                       'jobId': payload.get('jobId') or ('job_' + uuid.uuid4().hex), 'task': payload}
            status = submit(Path(__file__).resolve().parent, root, request)
            while status['state'] in ('queued', 'running'):
                status = inspect(root, status['jobId'], 'wait', status.get('cursor', 0), 30)
            if status['state'] != 'completed':
                raise RuntimeError(status.get('error', 'Isaac operation did not complete'))
            return status['result']
        return supervise(payload, required_files, JOB_DIRECTORY)
    if operation == 'prepare':
        return prepare_asset(payload, legacy, root)
    if operation == 'build':
        return build(payload, root)
    if operation == 'rollout':
        return rollout(payload, root)
    if operation == 'evaluate':
        return evaluate_internal(payload, root)
    if operation == 'inspect':
        return inspect_project(payload, root)
    if operation == 'observe':
        return observe(payload, root)
    if operation == 'check':
        return check(payload, root)
    if operation == 'consume':
        return consume_internal(payload, root)
    if operation == 'read':
        path = within(root, payload['path'])
        if path.stat().st_size > 10_000_000:
            raise ValueError('use artifact streaming for files larger than 10 MB')
        return {'path': payload['path'], 'content': path.read_text()}
    if operation == 'write':
        path = within(root, payload['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(payload['content'])
        temporary.replace(path)
        return {'path': payload['path'], 'sha256': digest(path)}
    if operation == 'list':
        return {'projects': [{**json.loads(path.read_text()),
            'lastEpisode': json.loads((path.parent / 'latest_episode.json').read_text())
                if (path.parent / 'latest_episode.json').exists() else None}
            for path in sorted((root / 'projects').glob('*/latest.json'))]}
    raise ValueError(f'unknown operation: {operation}')


if __name__ == '__main__':
    if '--mdl-audit' in sys.argv:
        from runtime.mdl import cpu_entry
        cpu_entry()
    elif '--native-stage-audit' in sys.argv:
        save(Path('/audit-output/result.json'), audit_native_stage('/resources', _collect_only=True))
    elif '--native-compile' in sys.argv:
        native_compile_entry()
    elif '--native-interface-episode' in sys.argv:
        native_interface_entry(Path(sys.argv[sys.argv.index('--native-interface-episode') + 1]))
    elif '--asset-source' in sys.argv:
        asset_source_entry()
    elif '--native-build-source' in sys.argv:
        native_build_entry()
    elif '--controller' in sys.argv:
        controller_entry()
    elif '--rubric' in sys.argv:
        rubric_entry()
    elif '--kit-worker' in sys.argv:
        kit_entry(Path(sys.argv[sys.argv.index('--kit-worker') + 1]))
    elif '--execute-job' in sys.argv:
        from runtime.jobs import execute
        JOB_DIRECTORY = Path(sys.argv[sys.argv.index('--execute-job') + 1])
        execute(JOB_DIRECTORY, run, finalize)
    else:
        try:
            payload = json.load(sys.stdin)
            emit('result', result=run(payload))
        except Exception as exc:
            emit('error', error=f'{type(exc).__name__}: {exc}', traceback=traceback.format_exc())
            sys.exit(1)
