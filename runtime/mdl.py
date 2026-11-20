"""Official locked MDL backend bridge. Importing this module never imports USD/Kit."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def _sdk():
    return Path(sys.prefix)/'lib/python3.12/site-packages/isaacsim'


def sdk_identity():
    sdk = _sdk()
    binding = sdk/'extscache/omni.mdl-57.0.3+6312fa25.lx64.r.cp312'
    backend = sdk/'extscache/omni.mdl.neuraylib-0.2.20+6312fa25.lx64.r.cp312'
    library = sdk/'extscache/omni.iray.libs-0.0.1+6312fa25.lx64.r/bin/iray/libneuray.so'
    paths = [library, binding/'omni/mdl/pymdlsdk/__init__.py',
        binding/'omni/mdl/pymdlsdk/_pymdlsdk.cpython-312-x86_64-linux-gnu.so',
        backend/'bin/deps/librtx.neuraylib.plugin.so',
        backend/'omni/mdl/neuraylib/_neuraylib.cpython-312-x86_64-linux-gnu.so',
        backend/'omni/mdl/neuraylib/utils/_inspect.py', backend/'omni/mdl/neuraylib/utils/utils.py',
        backend/'omni/mdl/neuraylib/scripts/entrypoints.py',
        library.with_name('iray_openimageio.so'),
        sdk/'extscache/omni.kit.usd.mdl-1.1.9+6312fa25.lx64.r.cp312/bin/deps/librtx.usdmdl.plugin.so',
        sdk/'extscache/omni.hydra.rtx-1.0.3+6312fa25.lx64.r/bin/deps/librtx.mdltranslator.plugin.so']
    return {str(path.relative_to(sys.prefix)): sha256(path) for path in paths}


def _within(root, name):
    path = root/Path(name)
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('MDL dependency leaves the frozen closure')
    for parent in [path, *path.parents]:
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ValueError('MDL dependency contains a symlink')
    return resolved


def file_identity(filename, directory):
    value = urlsplit(filename)
    if value.scheme and value.scheme != 'file' or value.netloc or value.query or value.fragment:
        raise ValueError('MDL dependency must resolve to a plain local file')
    path = Path(unquote(value.path) if value.scheme else filename)
    if not path.is_absolute():
        raise ValueError('MDL resolver did not return an absolute file')
    if path.is_symlink():
        raise ValueError('MDL dependency contains a symlink')
    path = path.resolve()
    directory, prefix = Path(directory).resolve(), Path(sys.prefix).resolve()
    if path.is_relative_to(directory):
        path = _within(directory, path.relative_to(directory))
        key = 'package/'+str(path.relative_to(directory))
    elif path.is_relative_to(_sdk()/'kit/mdl'):
        path = _within(prefix, path.relative_to(prefix))
        key = 'sdk/'+str(path.relative_to(prefix))
    else:
        raise ValueError('MDL resolver selected an undeclared external file: '+str(path))
    if not path.is_file():
        raise ValueError('MDL resolved dependency is missing: '+str(path))
    return {'key': key, 'sha256': sha256(path)}


def declared_roots(directory):
    directory = Path(directory).resolve()
    roots = []
    for report_path in sorted(directory.rglob('report.json')):
        # Prepared bundles, including the copied base of a robot installation.
        if 'dependencies/resources' not in str(report_path.relative_to(directory)):
            continue
        report = json.loads(report_path.read_text())
        if report.get('kind') != 'native_files':
            continue
        values = report.get('inputs', {}).get('mdlSearchRoots', [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError('invalid frozen MDL roots')
        for value in values:
            if Path(value).is_absolute() or '..' in Path(value).parts or value != Path(value).as_posix():
                raise ValueError('invalid frozen MDL root path')
            path = _within(report_path.parent, value)
            if not path.is_dir():
                raise ValueError('frozen MDL root is missing')
            relative = str(path.relative_to(directory))
            if relative not in roots:
                roots.append(relative)
    return roots


def semantic(report):
    return {'modules': sorted((row['key'], row.get('sha256'), tuple(sorted(row['imports'])))
                              for row in report['modules']),
            'resources': report['resources']}


def verify_source_identity(resolved, expected_key, files):
    if resolved['key'] != expected_key:
        # Equal entry bytes do not prove that relative imports/resources bind equally.
        raise ValueError('live MDL source alias location lacks qualified import/resource equivalence')
    if resolved['sha256'] != files.get(expected_key):
        raise ValueError('live MDL source digest changed')


def verify(audit, directory):
    """Pure JSON/file preflight, safe before the first native SDK import."""
    report = audit.get('mdl')
    if report is None:
        return None
    if audit.get('schema') != 'sceneagent-native-stage-audit-v2' or report.get('schema') != 'sceneagent-mdl-audit-v1':
        raise ValueError('unverified MDL audit schema')
    if report['sdk'] != sdk_identity():
        raise ValueError('locked MDL SDK identity changed')
    directory = Path(directory).resolve()
    for root in report['roots']:
        if not _within(directory, root).is_dir():
            raise ValueError('frozen MDL root changed or missing')
    for key, expected in report['files'].items():
        if key.startswith('package/'):
            relative = key[len('package/'):]
            path = _within(directory, relative)
            if audit['files'].get(relative) != expected:
                raise ValueError('MDL closure differs from the native file digest identity')
        elif key.startswith('sdk/'):
            path = _within(Path(sys.prefix), key[len('sdk/'):])
        else:
            raise ValueError('invalid MDL frozen file identity')
        if not path.is_file() or sha256(path) != expected:
            raise ValueError('frozen MDL dependency digest changed: '+key)
    return report


def read_audit(directory, filename='native-stage-audit.json'):
    path = _within(Path(directory).resolve(), filename)
    audit = json.loads(path.read_text())
    verify(audit, directory)
    return audit


def audit(directory, entries, command, timeout):
    """Use the existing controller sandbox without entering another job/GPU lock."""
    directory = Path(directory).resolve()
    request = {'directory': '/resources', 'entries': entries, 'roots': declared_roots(directory),
               'timeout': timeout}
    with tempfile.TemporaryDirectory(prefix='sceneagent-mdl-audit-') as name:
        output = Path(name)
        command = [*command[:-3], '--ro-bind', str(directory), '/resources',
                   '--bind', str(output), '/mdl-output', '--setenv', 'OMNI_KIT_ACCEPT_EULA', 'YES',
                   '--setenv', 'PXR_WORK_THREAD_LIMIT', '1', *command[-3:]]
        with (directory/'mdl-audit.log').open('wb') as log:
            result = subprocess.run(command, input=json.dumps(request).encode(), stdout=log,
                                    stderr=subprocess.STDOUT, timeout=timeout, close_fds=True)
        if result.returncode or not (output/'result.json').is_file():
            raise ValueError('official MDL closure audit failed; see mdl-audit.log')
        report = json.loads((output/'result.json').read_text())
    if report.get('schema') != 'sceneagent-mdl-audit-v1' or report.get('sdk') != sdk_identity():
        raise ValueError('MDL subprocess returned a different SDK identity')
    if report['roots'] != request['roots'] or report['entries'] != entries:
        raise ValueError('MDL subprocess changed the declared roots or entries')
    return report


@contextmanager
def _handle(value):
    # The locked SWIG __exit__ masks Python exceptions with a void* conversion error.
    # Explicit release preserves the real failure and the SDK's reference ownership.
    try:
        yield value
    finally:
        value.release()


@contextmanager
def _transaction(backend, sdk, scope):
    transaction = sdk.attach_itransaction(backend.createReadingTransaction(scope))
    try:
        yield transaction
    finally:
        # This transaction is explicitly borrowed for read access; never commit it.
        transaction.abort()
        transaction.release()


@contextmanager
def _cpu_sdk(roots):
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    kit = _sdk()/'kit'
    spec = importlib.util.spec_from_file_location('kit_app', kit/'kit_app.py')
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    app = bootstrap.KitApp()
    config = Path('/tmp/mdl-audit.kit')
    config.write_text('[dependencies]\n"omni.mdl.neuraylib" = {}\n[settings]\n'
        'app.window.enabled = false\napp.extensions.registryEnabled = false\n'
        'app.extensions.syncRegistryOnStartup = false\napp.settings.persistent = false\n'
        'renderer.enabled = ""\napp.mdl.additionalSystemPaths = '+json.dumps(roots)+'\n')
    app.startup([str(config), '--ext-folder', str(_sdk()/'extscache'), '--ext-folder', str(kit/'exts'),
        '--no-window', '--/app/fastShutdown=false', '--/log/file=/tmp/mdl-audit.log'])
    try:
        for forbidden in ('omni.hydra.rtx', 'omni.kit.renderer.core', 'omni.physx'):
            if app.extension_manager.get_enabled_extension_id(forbidden):
                raise ValueError('CPU MDL audit unexpectedly enabled a renderer or physics')
        import omni.mdl.neuraylib as backend
        import omni.mdl.pymdlsdk as sdk
        backend.ensure_running()
        yield backend, sdk
    finally:
        app.shutdown()


class _Inspector:
    """One transaction's official dependency walk, shared by CPU and real RTX."""
    def __init__(self, directory, roots, backend, sdk, transaction):
        self.directory, self.roots = Path(directory), roots
        self.backend, self.sdk, self.tx = backend, sdk, transaction
        self.modules, self.resources, self.files, self.visited = {}, {}, {}, {}

    def file(self, filename):
        record = file_identity(filename, self.directory)
        self.files[record['key']] = record['sha256']
        return record

    def resource(self, value):
        if not value.get_file_path() and not value.get_value():
            return
        filenames = self.backend.get_resource_uri_list(self.tx, value, True)
        if not filenames or any(name.startswith('[missing]') for name in filenames):
            raise ValueError('MDL resource is missing: '+str(value.get_file_path()))
        for filename in filenames:
            record = self.file(filename)
            self.resources[record['key']] = record['sha256']

    def module(self, db_name):
        if db_name in self.visited:
            return self.visited[db_name]
        with _handle(self.tx.access_as(self.sdk.IModule, db_name)) as module:
            if not module.is_valid_interface():
                raise ValueError('MDL imported module is unavailable: '+db_name)
            filename = module.get_filename()
            record = self.file(filename) if filename else {'key':'builtin/'+module.get_mdl_name()}
            key = record['key']
            self.visited[db_name] = key
            row = {**record, 'dbName':db_name, 'mdlName':module.get_mdl_name(),
                   'filename':filename, 'imports':[]}
            previous = self.modules.get(key)
            self.modules[key] = row
            if filename:
                # Only a module actually using a global declared name can conflict.
                relative = module.get_mdl_name().removeprefix('::').replace('::', '/')+'.mdl'
                candidates = [Path(root)/relative for root in self.roots]
                candidates += [_sdk()/'kit/mdl/core/Base'/relative, _sdk()/'kit/mdl/core/mdl'/relative]
                identities = [self.file(str(path)) for path in candidates if path.is_file()]
                if len({item['sha256'] for item in identities}) > 1:
                    raise ValueError('ambiguous MDL absolute import: '+module.get_mdl_name())
                row['sameContentCandidates'] = identities
            for i in range(module.get_resources_count()):
                with _handle(module.get_resource(i)) as resource:
                    self.resource(resource)
            names = [module.get_import(i) for i in range(module.get_import_count())]
        row['imports'] = [self.module(name) for name in names]
        if previous is not None and (previous.get('sha256'), sorted(previous['imports'])) != (row.get('sha256'), sorted(row['imports'])):
            raise ValueError('the same MDL source resolved with different import bindings')
        return key

    def parameters(self, entry, module_db):
        with _handle(self.tx.access_as(self.sdk.IModule, module_db)) as module:
            definitions = [module.get_material(i) for i in range(module.get_material_count())]
            definitions += [module.get_function(i) for i in range(module.get_function_count())]
            matches = []
            for name in definitions:
                with _handle(self.tx.access_as(self.sdk.IFunction_definition, name)) as definition:
                    if definition.get_mdl_simple_name() == entry['subIdentifier']:
                        matches.append(name)
            if len(matches) != 1:
                raise ValueError('MDL source subIdentifier is missing or ambiguous')
        with _handle(self.tx.access_as(self.sdk.IFunction_definition, matches[0])) as definition, _handle(definition.get_parameter_types()) as types:
            for parameter in entry['parameters']:
                value = parameter['asset']
                if not value:
                    continue
                with _handle(types.get_type(parameter['name'])) as alias, _handle(alias.skip_all_type_aliases()) as type_:
                    if not type_.is_valid_interface() or type_.get_kind() != self.sdk.IType.Kind.TK_TEXTURE:
                        raise ValueError('MDL asset parameter type is not qualified: '+parameter['name'])
                parsed = urlsplit(value)
                if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or '\\' in value:
                    raise ValueError('MDL parameter must use a frozen local relative asset')
                filename = Path(value) if Path(value).is_absolute() else self.directory/parameter.get('layer', entry['layer'])
                if not Path(value).is_absolute():
                    filename = filename.parent/value
                filename = _within(self.directory, filename)
                with _handle(self.tx.create_as(self.sdk.IImage, 'Image')) as image:
                    if image.reset_file(str(filename)) != 0:
                        raise ValueError('MDL texture parameter is missing or unsupported: '+parameter['name'])
                    for frame in range(image.get_length()):
                        for tile in range(image.get_frame_length(frame)):
                            record = self.file(image.get_filename(frame, tile))
                            self.resources[record['key']] = record['sha256']

    def report(self):
        return {'modules':sorted(self.modules.values(), key=lambda row:row['key']),
                'resources':self.resources, 'files':self.files}


def _inspect_sources(directory, roots, entries, backend, sdk):
    directory = Path(directory)
    paths = [str(_within(directory, name)) for name in roots]
    ov = backend.get_neuraylib()
    result = {'schema':'sceneagent-mdl-audit-v1', 'sdk':sdk_identity(), 'roots':roots,
              'entries':entries, 'entryBindings':[], 'modules':[], 'resources':{}, 'files':{}}
    inspected = {}
    for entry in entries:
        descriptor = {name:value for name,value in entry.items() if name not in ('shader','effectivePrimPath')}
        cache_key = json.dumps(descriptor, sort_keys=True)
        if cache_key not in inspected:
            asset = entry['asset']
            if '/' in asset or Path(asset).is_absolute():
                filename = Path(asset) if Path(asset).is_absolute() else directory/entry['layer']
                if not Path(asset).is_absolute():
                    filename = filename.parent/asset
                asset = _within(directory, filename).as_uri()
            module = ov.createMdlModule(asset)
            try:
                if not module or not module.valid():
                    raise ValueError('official MDL module could not be loaded: '+asset)
                with _transaction(ov, sdk, module.dbScopeName) as tx:
                    inspector = _Inspector(directory, paths, backend, sdk, tx)
                    key = inspector.module(module.dbName)
                    if entry['subIdentifier'] or entry.get('effectivePrimPath'):
                        inspector.parameters(entry, module.dbName)
                    inspected[cache_key] = (key, module.dbName, inspector.report())
            finally:
                if module:
                    ov.destroyMdlModule(module)
        key, db_name, current = inspected[cache_key]
        if entry.get('effectivePrimPath'):
            result['entryBindings'].append({'shader':entry['effectivePrimPath'], 'key':key,
                'subIdentifier':entry['subIdentifier'], 'dbName':db_name, 'resources':current['resources']})
        result['modules'] += current['modules']
        result['resources'].update(current['resources'])
        result['files'].update(current['files'])
    unique = {}
    for row in result['modules']:
        prior = unique.get(row['key'])
        if prior is not None and (prior.get('sha256'), sorted(prior['imports'])) != (row.get('sha256'), sorted(row['imports'])):
            raise ValueError('MDL entrypoints resolved the same file with different imports')
        unique[row['key']] = row
    result['modules'] = sorted(unique.values(), key=lambda row:row['key'])
    result['entryBindings'].sort(key=lambda row:row['shader'])
    return result


def cpu_entry():
    request = json.load(sys.stdin)
    directory = Path(request['directory'])
    roots = [str(_within(directory, name)) for name in request['roots']]
    with _cpu_sdk(roots) as (backend, sdk):
        result = _inspect_sources(directory, request['roots'], request['entries'], backend, sdk)
        Path('/mdl-output/result.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False))


def verify_native_materials(stage, directory, audit):
    """Recheck official source resolution in the GPU process; scope renderer observations separately."""
    expected = verify(audit, directory)
    if expected is None:
        return None
    import omni.mdl.neuraylib as backend
    import omni.mdl.pymdlsdk as sdk
    from pxr import Usd, UsdGeom, UsdShade
    backend.ensure_running()
    ov = backend.get_neuraylib()
    actual = _inspect_sources(directory, expected['roots'], expected['entries'], backend, sdk)
    if semantic(actual) != semantic(expected):
        raise ValueError('native MDL source resolution differs from the frozen CPU closure')
    def bindings_by_shader(report):
        return {row['shader']:(row['key'],row['subIdentifier'],row['resources']) for row in report['entryBindings']}
    if bindings_by_shader(actual) != bindings_by_shader(expected):
        raise ValueError('native MDL source entry bindings differ from the frozen CPU audit')
    bindings = {row['shader']:row for row in expected['entryBindings']}
    # The live USD resolver is another real source consumer, independent of the MDL module DB.
    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
        attribute = prim.GetAttribute('info:mdl:sourceAsset')
        if not attribute:
            continue
        binding = bindings.get(str(prim.GetPath()))
        identifier = str(prim.GetAttribute('info:mdl:sourceAsset:subIdentifier').Get() or '')
        if not binding or identifier != binding['subIdentifier']:
            raise ValueError('live MDL shader source differs from the frozen effective entry')
        asset = attribute.Get()
        if asset.resolvedPath:
            resolved = file_identity(asset.resolvedPath, directory)
            verify_source_identity(resolved, binding['key'], expected['files'])
    # createMdlEntity is a renderer-owned mapping, not an independent USD parser.
    # Unused libraries, instance proxies or materials not rendered yet may have no mapping.
    bound = {}
    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
        if not prim.IsA(UsdGeom.Mesh):
            continue
        imageable = UsdGeom.Imageable(prim)
        if imageable.ComputeVisibility() == 'invisible' or imageable.ComputePurpose() not in ('default','render'):
            continue
        material = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
        if not material:
            continue
        shader = material.ComputeSurfaceSource('mdl')[0] or material.ComputeSurfaceSource()[0]
        if shader and shader.GetPrim().GetAttribute('info:mdl:sourceAsset'):
            bound[str(shader.GetPath())] = shader.GetPrim()
    observed, unknown = [], []
    roots = [str(_within(Path(directory), name)) for name in expected['roots']]
    for path, prim in bound.items():
        entity = ov.createMdlEntity(path, ov.getCurrentDefaultScope())
        snapshot = None
        try:
            if not entity or not entity.valid():
                unknown.append({'shader':path, 'reason':'renderer entity not available', 'instanceProxy':prim.IsInstanceProxy()})
                continue
            snapshot = ov.createMdlEntitySnapshot(entity)
            if not snapshot or not snapshot.dbName:
                unknown.append({'shader':path, 'reason':'renderer function snapshot not available'})
                continue
            with _transaction(ov, sdk, snapshot.dbScopeName) as tx:
                with _handle(tx.access_as(sdk.IFunction_call, snapshot.dbName)) as call:
                    if not call.is_valid_interface():
                        unknown.append({'shader':path, 'reason':'renderer snapshot is not an inspectable function call'})
                        continue
                    with _handle(tx.access_as(sdk.IFunction_definition, call.get_function_definition())) as definition:
                        module_name = definition.get_module()
                        function_name = definition.get_mdl_simple_name()
                with _handle(tx.access_as(sdk.IModule, module_name)) as module:
                    if not module.get_filename():
                        unknown.append({'shader':path, 'reason':'renderer uses an SDK-generated definition', 'function':function_name})
                        continue
                inspector = _Inspector(directory, roots, backend, sdk, tx)
                key = inspector.module(module_name)
                binding = bindings[path]
                if (key, function_name) != (binding['key'], binding['subIdentifier']):
                    raise ValueError('observed renderer MDL function differs from the frozen shader entry: '+path)
                for resource in backend.get_graph_resources(tx, snapshot.dbName):
                    with _handle(resource) as value:
                        inspector.resource(value)
                if inspector.resources != binding['resources']:
                    raise ValueError('observed renderer MDL resources differ from the frozen shader entry: '+path)
                observed.append({'shader':path,'key':key,'function':function_name,
                                 'resources':inspector.resources,'dbName':snapshot.dbName})
        finally:
            if snapshot: ov.destroyMdlEntitySnapshot(snapshot)
            if entity: ov.destroyMdlEntity(entity)
    return {'schema':'sceneagent-mdl-rendering-v1',
        'sourceResolution':{'verified':True, 'modules':actual['modules'], 'entryBindings':actual['entryBindings'],
                            'resources':actual['resources'], 'scope':'official source resolver in the renderer process'},
        'rendererMapping':{'status':'partial' if observed and unknown else 'observed' if observed else 'unknown',
                           'observed':observed, 'unobserved':unknown,
                           'scope':'visible bound shader mappings available at inspection; unrendered definitions are not required'}}
