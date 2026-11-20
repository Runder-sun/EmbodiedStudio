"""Read-only source libraries, a disposable search index and frozen prepared assets."""
import contextlib
import hashlib
import importlib.metadata
import io
import json
import math
from numbers import Integral
from pathlib import Path
import re
import shlex
import sqlite3
import struct
import sys
import time
import uuid

from runtime.records import atomic_json

PREPARATION_VERSION = 'sceneagent-assets-v5'
SOURCE_INDICES = ('data/objathor-assets/preprocessed/metadata_index.json',
                  'assets/ready_asset_manifest.json', 'assets/generated_asset_manifest.json',
                  'assets/objaverse/index.json', 'data/artvip_sdf/index.json',
                  'data/materials/sceneagent-material-index.json')
TRANSLATIONS = {'办公椅': 'office chair', '扶手椅': 'armchair', '椅子': 'chair', '椅': 'chair',
                '文件柜': 'file cabinet', '柜子': 'cabinet', '柜': 'cabinet', '桌子': 'table',
                '书桌': 'desk', '桌': 'table', '台灯': 'table lamp', '灯': 'lamp',
                '木地板': 'wood floor', '木纹': 'wood', '木': 'wood', '皮革': 'leather',
                '布料': 'fabric', '金属': 'metal', '瓷砖': 'tiles', '墙灰': 'plaster',
                '黑色': 'black', '白色': 'white', '棕色': 'brown'}
TRANSLATIONS.update({'洗衣机': 'washing machine', '冰箱': 'refrigerator', '抽屉': 'drawer',
                     '衣柜': 'wardrobe', '床头柜': 'bedside cabinet'})
CATEGORY_TERMS = {
    'lamp': ('lamp', 'lamps', 'lighting', 'table lamp', 'floor lamp', 'desk lamp', 'bedside lamp', 'nightstand lamp'),
    'chair': ('chair', 'chairs', 'armchair', 'armchairs', 'stool', 'stools', 'seating', 'seat', 'seats', 'office chair'),
    'cabinet': ('cabinet', 'cabinets', 'dresser', 'bookcase', 'cupboard', 'storage', 'wardrobe', 'wardrobes', 'closet', 'nightstand', 'nightstands'),
    'desk': ('desk', 'desks', 'workbench'), 'table': ('table', 'tables', 'dining table'),
    'sofa': ('sofa', 'sofas', 'couch', 'couches'), 'bed': ('bed', 'beds'), 'bottle': ('bottle', 'bottles'),
    'bowl': ('bowl', 'bowls'), 'tool': ('tool', 'tools', 'knife', 'wrench'),
    'refrigerator': ('refrigerator', 'refrigerators', 'fridge', 'fridges'),
    'washing machine': ('washing machine', 'washing machines', 'washer', 'washers'),
    'drawer': ('drawer', 'drawers'), 'door': ('door', 'doors'), 'handle': ('handle', 'handles')}
CATEGORY_ALIASES = {term: category for category, terms in CATEGORY_TERMS.items() for term in terms}
QUERY_ALIASES = {term: term[:-1] for term in CATEGORY_ALIASES if term.endswith('s') and term[:-1] in CATEGORY_ALIASES}
QUERY_ALIASES.update({'fridge': 'refrigerator', 'fridges': 'refrigerator',
                      'refrigerators': 'refrigerator', 'washer': 'washing machine', 'washers': 'washing machine',
                      'couch': 'sofa', 'couches': 'sofa', 'bedside table': 'nightstand', 'bedside tables': 'nightstand',
                      'bedside cabinet': 'nightstand', 'bedside cabinets': 'nightstand', 'table bedside': 'nightstand'})
QUERY_ALIAS_PATTERN = re.compile(r'\b(?:' + '|'.join(re.escape(alias) for alias in sorted(QUERY_ALIASES, key=len, reverse=True)) + r')\b')
ASSET_METADATA = ('jointCount', 'movableJointCount', 'jointTypes', 'jointEvidence', 'objectRole',
                  'sourceRole', 'sourceObjectType', 'objectGroup', 'technicalEvidence')


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(value):
    return _hash(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())


def _versions():
    result = {'python': sys.version.split()[0], 'algorithm': PREPARATION_VERSION,
              'codeDigest': _hash(Path(__file__).read_bytes()),
              'rendererDigest': _hash(Path(__file__).with_name('simulation.py').read_bytes())}
    for name in ('isaacsim', 'isaacsim-core', 'torch', 'trimesh', 'numpy', 'Pillow', 'imageio', 'imageio-ffmpeg'):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = 'unavailable'
    return result


def _normalize(value):
    value = re.sub(r'([a-z])([A-Z])', r'\1 \2', str(value))
    value = re.sub(r'(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])', ' ', value)
    return ' '.join(re.findall(r'[^\W_]+', value.lower()))


def _query(value):
    for chinese in sorted(TRANSLATIONS, key=len, reverse=True):
        value = value.replace(chinese, ' ' + TRANSLATIONS[chinese] + ' ')
    return QUERY_ALIAS_PATTERN.sub(lambda match: QUERY_ALIASES[match.group()], _normalize(value))


def _category(text, fallback=''):
    for part in reversed(re.split(r'[/|>:]', str(fallback))):
        declared = _query(part)
        if declared in CATEGORY_ALIASES:
            return CATEGORY_ALIASES[declared]
        if declared.split() and declared.split()[-1] in CATEGORY_ALIASES:
            return CATEGORY_ALIASES[declared.split()[-1]]
    # The head object precedes accessories: a cabinet with a lamp remains a cabinet.
    head = _query(text).split(' with ')[0]
    if any(phrase in head for phrase in ('table lamp', 'floor lamp', 'desk lamp')):
        return 'lamp'
    for phrase, category in CATEGORY_ALIASES.items():
        if ' ' in phrase and ' ' + phrase + ' ' in ' ' + head + ' ':
            return category
    words = head.split()
    for word in words:
        if word in CATEGORY_ALIASES:
            return CATEGORY_ALIASES[word]
    return fallback


def _dimensions(value):
    return list(value) if (isinstance(value, (list, tuple)) and len(value) == 3 and all(
        type(number) in (int, float) and math.isfinite(number) and number > 0 for number in value)) else None


def _asset_metadata(row):
    size = _dimensions(row.get('size', row.get('dimensions') or row.get('bbox')))
    evidence = row.get('jointEvidence')
    evidence = dict(evidence) if isinstance(evidence, dict) else {'status': 'source_declared' if 'joint_count' in row else 'unknown'}
    result = {key: row[key] for key in ('sourceRole', 'sourceObjectType', 'objectGroup', 'technicalEvidence') if key in row}
    for key, value in (('jointCount', row.get('jointCount', row.get('joint_count'))),
                       ('movableJointCount', row.get('movableJointCount'))):
        result[key] = value if type(value) is int and value >= 0 else None
    types = row.get('jointTypes', [])
    result.update(jointTypes=list(types) if isinstance(types, list) and all(isinstance(value, str) for value in types) else [],
        jointEvidence=evidence, objectRole=row.get('objectRole') if row.get('objectRole') in ('whole', 'part', 'unknown') else 'unknown',
        size=size, sizeUnits=row.get('sizeUnits', row.get('units', 'unknown')),
        sizeAxis=row.get('sizeAxis', row.get('upAxis', row.get('up_axis', 'unknown'))),
        sizeEvidence=row.get('sizeEvidence', 'source_metadata') if size else 'unknown')
    return result


def _joint_state(row):
    count = row.get('movableJointCount')
    evidence = row.get('jointEvidence')
    if (type(count) is int and count >= 0 and isinstance(evidence, dict)
            and evidence.get('status') == 'source_inspected'
            and all(isinstance(evidence.get(key), str) and evidence[key].strip()
                    for key in ('method', 'sourceRef', 'sourceRevision'))
            and evidence['sourceRevision'] == row.get('sourceRevision')
            and isinstance(evidence.get('sourceDigest'), str)
            and re.fullmatch('[0-9a-fA-F]{64}', evidence['sourceDigest'])):
        return 'movable' if count else 'none'
    return 'unknown'


def _path(legacy, value):
    path = Path(value)
    return path if path.is_absolute() else legacy / path


def _license(value):
    return 'unknown' if not value or str(value).lower() in ('objathor', 'objaverse', 'artvip', 'hssd', 'scenesmith_articulated') else value


def _row(legacy, row, index):
    identifier = str(row.get('asset_id') or row.get('uid') or row.get('sha256') or row.get('relative_path'))
    source = row.get('source') or ('objaverse' if index.startswith('assets/objaverse') else 'legacy')
    path = row.get('visual_asset_path') or row.get('visual_mesh_path') or row.get('glb_path') or row.get('relative_path') or row.get('visual_path') or row.get('sdf_path')
    if not path or '.supportsurface' in str(path).lower() or row.get('metadata', {}).get('hssd_sidecar_kind'):
        return None
    description = row.get('description') or row.get('query') or row.get('name') or row.get('category') or ''
    return {'id': identifier, 'name': row.get('name') or row.get('query') or description,
            'description': description, 'category': _category(description,
                _category(row.get('name', ''), row.get('category') or row.get('internal_category') or '')),
            'sourceCategory': row.get('category') or row.get('internal_category') or '',
            'path': str(_path(legacy, path)), 'source': source, 'kind': 'model',
            'upAxis': row.get('up_axis') or ('Y' if source in ('objaverse', 'objathor', 'hssd') else 'Z'),
            'units': row.get('units', 'unknown'), 'affordances': row.get('affordances', []),
            'license': _license(row.get('license', '')), 'sourceDataset': source,
            'sourceLicenseLabel': row.get('license', ''), 'sourceRevision': row.get('sourceRevision', row.get('revision')), 'index': index,
            'materialInfo': row.get('material_info', {}), 'aliases': row.get('aliases', []), **_asset_metadata(row)}


def _collection_path(base, relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative
            or Path(relative).is_absolute() or '..' in Path(relative).parts or str(Path(relative)) != relative):
        raise ValueError('collection paths must be normalized relative paths')
    base = Path(base)
    current = base
    for part in (None, *Path(relative).parts):
        if part is not None:
            current = current / part
        if current.is_symlink():
            raise ValueError('collection source cannot contain a symlink')
    if not current.resolve().is_relative_to(base.resolve()):
        raise ValueError('collection source escapes its declared source root')
    return current.resolve()


def _collection_rows(legacy, root):
    if root is None:
        return
    catalog = _collection_path(root / 'assets', 'collections/catalog.json')
    if not catalog.exists():
        return
    document = json.loads(catalog.read_text())
    if (not isinstance(document, dict) or document.get('schema') != 'sceneagent-asset-collections-v1'
            or not isinstance(document.get('collections'), list) or not isinstance(document.get('assets'), list)):
        raise ValueError('invalid collection catalog')
    collections = {}
    for collection in document['collections']:
        if not isinstance(collection, dict):
            raise ValueError('collection metadata must be an object')
        identifier = collection.get('id')
        if (not isinstance(identifier, str) or not identifier or identifier in collections
                or collection.get('libraryTier') not in ('preferred', 'broad')):
            raise ValueError('invalid or duplicate collection identity/tier')
        collections[identifier] = collection
    roots = {'current_assets': root.resolve() / 'assets', 'legacy_assets': legacy.resolve() / 'assets',
             'legacy_data': legacy.resolve() / 'data'}
    identifiers = set()
    for raw in document['assets']:
        if not isinstance(raw, dict):
            raise ValueError('collection asset must be an object')
        identifier = raw.get('assetId')
        collection = collections.get(raw.get('collection'))
        if (not isinstance(identifier, str) or not identifier.startswith('collection:') or identifier in identifiers
                or collection is None or raw.get('format') != 'native_files' or raw.get('kind', 'model') not in ('model', 'material')):
            raise ValueError('invalid collection asset identity or format')
        identifiers.add(identifier)
        source_root = roots.get(raw.get('sourceRoot'))
        if source_root is None:
            raise ValueError('collection asset requires a declared source root')
        package = _collection_path(source_root, raw.get('packageRoot'))
        entry, dependencies = raw.get('entry'), raw.get('dependencyFiles', [])
        if (not isinstance(entry, str) or not isinstance(dependencies, list)
                or any(not isinstance(name, str) for name in dependencies)
                or len(set([entry, *dependencies])) != len(dependencies) + 1):
            raise ValueError('invalid or duplicate collection dependency files')
        paths = [_collection_path(package, name) for name in [entry, *dependencies]]
        license_evidence = raw.get('licenseEvidence', collection.get('licenseEvidence', {'status': 'unknown', 'value': 'unknown'}))
        if not isinstance(license_evidence, dict):
            raise ValueError('collection license evidence must be an object')
        yield {'id': identifier, 'name': raw.get('name') or identifier,
            'description': raw.get('description') or raw.get('name') or identifier,
            'category': _category(raw.get('description', ''), _category(raw.get('name', ''), raw.get('category', ''))),
            'sourceCategory': raw.get('category', ''),
            'aliases': raw.get('aliases', []), 'source': collection['id'], 'sourceDataset': collection['id'],
            'kind': raw.get('kind', 'model'), 'format': 'native_files', 'path': str(paths[0]),
            'sourceRootPath': str(source_root), 'packageRelative': raw['packageRoot'], 'packageRoot': str(package),
            'entry': entry, 'dependencyFiles': dependencies, 'fileCount': len(paths),
            'upAxis': raw.get('upAxis', 'unknown'), 'units': raw.get('units', 'unknown'),
            'collection': collection['id'], 'collectionRevision': collection.get('revision'),
            'sourceRevision': raw.get('sourceRevision', raw.get('revision', collection.get('revision'))),
            'sourcePage': raw.get('sourcePage', collection.get('sourcePage')), 'libraryTier': collection['libraryTier'],
            **({'mdlSearchRoots': raw['mdlSearchRoots']} if 'mdlSearchRoots' in raw else {}),
            'license': _license(license_evidence.get('value')), 'licenseEvidence': license_evidence,
            'collectionQualityEvidence': collection.get('qualityEvidence', {}),
            'qualityEvidence': raw.get('qualityEvidence', {'status': 'not_individually_reviewed'}),
            'importRequirements': raw.get('importRequirements', {}), 'index': str(catalog), **_asset_metadata(raw)}


def _source_rows(legacy, root):
    metadata = legacy / SOURCE_INDICES[0]
    if metadata.exists():
        for uid, raw in json.loads(metadata.read_text()).items():
            description = raw.get('description') or raw.get('name') or uid
            base = legacy / 'data/objathor-assets/assets' / uid
            yield {'id': 'objathor:' + uid, 'name': description, 'description': description,
                   'category': _category(description, raw.get('category', '')), 'path': str(base / (uid + '.pkl.gz')),
                   'source': 'objathor', 'kind': 'model', 'upAxis': 'Y', 'units': 'meters', 'size': None,
                   'sizeEvidence': 'unknown', 'affordances': [], 'license': 'unknown',
                   'sourceDataset': 'objathor', 'sourceLicenseLabel': raw.get('license', ''),
                   'index': SOURCE_INDICES[0], 'materialInfo': {key: str(base / filename) for key, filename in
                   (('albedo_path', 'albedo.jpg'), ('normal_path', 'normal.jpg'), ('emission_path', 'emission.jpg'))},
                   'sourceMetadata': raw, **{key: value for key, value in _asset_metadata(raw).items()
                       if key in ASSET_METADATA}, 'sizeUnits': 'meters', 'sizeAxis': 'Y'}
    for index in SOURCE_INDICES[1:]:
        path = legacy / index
        if not path.exists():
            continue
        document = json.loads(path.read_text())
        if index.endswith('sceneagent-material-index.json'):
            for raw in document:
                yield {'id': raw['material_id'], 'name': raw['name'], 'description': ' '.join(raw.get('tags', [])),
                       'category': raw.get('category', ''), 'kind': 'material', 'source': 'ambientcg',
                       'path': str(path), 'maps': {key: str(_path(legacy, value)) for key, value in raw.get('textures', {}).items()},
                       'license': raw.get('license', ''), 'index': index, 'size': None, 'sizeEvidence': 'not_applicable'}
            continue
        rows = document.get('records', document.get('assets')) if isinstance(document, dict) else document
        if rows is None:
            rows = [row for values in document.values() if isinstance(values, list) for row in values]
        for raw in rows:
            row = _row(legacy, raw, index)
            if row:
                yield row
    # A minimal old catalog remains a compatibility fallback only when primary sources are absent.
    if not any((legacy / index).exists() for index in SOURCE_INDICES):
        catalog = legacy / 'data/resource_closure/asset_catalog.json'
        if catalog.exists():
            for raw in json.loads(catalog.read_text()).get('entries', []):
                row = _row(legacy, raw, str(catalog))
                if row:
                    yield row
    yield from _collection_rows(legacy, root)


def _prepared_row(report, versions=None):
    source = report.get('source', {})
    source = source if isinstance(source, dict) else {'kind': source}
    metadata = report.get('inputs', {}).get('sourceMetadata', {})
    metadata = metadata if isinstance(metadata, dict) else {}
    native_metadata = metadata if report['kind'] == 'native_files' and metadata.get('collection') else {}
    measured = _dimensions(report.get('size'))
    native_dimensions = _asset_metadata(native_metadata)
    return {'id': report['assetId'], 'name': report.get('name') or report['assetId'],
            'description': report.get('description') or report.get('name') or report['assetId'],
            'category': report.get('category') or _category(report.get('name', '') + ' ' + report.get('description', '')),
            'source': source.get('kind', 'prepared'), 'kind': 'material' if report['kind'] == 'material' else 'model',
            'path': report.get('path') or report['reportPath'], 'reportPath': report['reportPath'],
            'size': measured or native_dimensions['size'],
            'sizeAxis': report.get('sizeAxis', 'Z') if measured else native_dimensions['sizeAxis'],
            'sizeUnits': report.get('sizeUnits') if measured else native_dimensions['sizeUnits'],
            'sizeEvidence': 'measured' if measured else (
                native_dimensions['sizeEvidence'] if report['kind'] == 'native_files' else 'not_applicable'),
            'upAxis': report.get('upAxis', native_metadata.get('upAxis')),
            'units': report.get('units', native_metadata.get('units')), 'license': _license(report.get('license', '')),
            'licenseEvidence': source.get('licenseEvidence', {'status': 'unknown', 'value': _license(report.get('license', ''))}),
            'prepared': True, 'preparationVersion': report.get('inputs', {}).get('versions', {}).get('algorithm'),
            'currentPreparation': report.get('inputs', {}).get('versions') == (versions if versions is not None else _versions()),
            'maps': report.get('maps', {}), 'textureEvidence': report.get('textureEvidence', {}),
            'thumbnailPath': report.get('thumbnailPath'), 'thumbnail': report.get('thumbnail', {}),
            'collisionFidelity': report.get('collisionFidelity'),
            'articulationPrepared': report.get('articulationPrepared', False),
            **{key: value for key, value in _asset_metadata(metadata).items() if key in ASSET_METADATA},
            **{key: metadata[key] for key in ('collection', 'collectionRevision', 'sourceRevision', 'collectionQualityEvidence',
                'libraryTier', 'qualityEvidence', 'importRequirements', 'sourcePage', 'sourceDataset', 'sourceCategory') if key in metadata},
            **({'format': 'native_files', 'entry': report.get('entry'),
                'fileCount': len(report.get('nativeFiles', []))} if report['kind'] == 'native_files' else {})}


def _index_schema(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
    connection.execute('CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, kind TEXT, category TEXT, payload TEXT, library_tier TEXT NOT NULL DEFAULT \'broad\', collection TEXT, joint_state TEXT NOT NULL DEFAULT \'unknown\')')
    columns = {row[1] for row in connection.execute('PRAGMA table_info(assets)')}
    for name, definition in (('library_tier', "TEXT NOT NULL DEFAULT 'broad'"),
                             ('collection', 'TEXT'), ('joint_state', "TEXT NOT NULL DEFAULT 'unknown'")):
        if name not in columns:
            connection.execute('ALTER TABLE assets ADD COLUMN ' + name + ' ' + definition)
    connection.execute('CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED, text)')


def _index(legacy, root, versions=None):
    versions = _versions() if versions is None else versions
    if root is None:
        connection = sqlite3.connect(':memory:')
        fingerprint = None
    else:
        directory = root / 'library'
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(directory / 'assets-v3.sqlite', timeout=60)
        signature = []
        for index in SOURCE_INDICES:
            path = legacy / index
            if path.exists():
                stat = path.stat()
                signature.append((index, stat.st_size, stat.st_mtime_ns))
        if not signature:
            path = legacy / 'data/resource_closure/asset_catalog.json'
            if path.exists():
                stat = path.stat()
                signature.append((str(path), stat.st_size, stat.st_mtime_ns))
        catalog = _collection_path(root / 'assets', 'collections/catalog.json')
        if catalog.exists():
            signature.append((str(catalog), _file_hash(catalog)))
        fingerprint = _json_hash({'indexSchema': 12, 'legacy': str(legacy.resolve()), 'files': signature})
    _index_schema(connection)
    previous = connection.execute("SELECT value FROM metadata WHERE key='sources'").fetchone()
    if fingerprint is None or previous is None or previous[0] != fingerprint:
        with connection:
            connection.execute('DELETE FROM assets')
            connection.execute('DELETE FROM search')
            for row in _source_rows(legacy, root):
                _insert(connection, row)
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('sources', ?)", (fingerprint or 'memory',))
            connection.execute("DELETE FROM metadata WHERE key IN ('prepared', 'prepared_ids')")
    if root:
        _sync_prepared(connection, root, versions)
    return connection


def _insert(connection, row):
    connection.execute('INSERT INTO assets (id,kind,category,payload,library_tier,collection,joint_state) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, category=excluded.category, payload=excluded.payload, library_tier=excluded.library_tier, collection=excluded.collection, joint_state=excluded.joint_state',
                       (row['id'], row['kind'], _normalize(row['category']), json.dumps(row, ensure_ascii=False),
                        row.get('libraryTier', 'broad'), row.get('collection'), _joint_state(row)))
    row_id = connection.execute('SELECT rowid FROM assets WHERE id=?', (row['id'],)).fetchone()[0]
    connection.execute('DELETE FROM search WHERE rowid=?', (row_id,))
    text = ' '.join(str(row.get(key, '')) for key in ('name', 'description', 'category', 'aliases', 'preparationSearchText'))
    connection.execute('INSERT INTO search (rowid,id,text) VALUES (?,?,?)', (row_id, row['id'], _query(text)))


def _read_report(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _report_metadata_valid(report):
    if not isinstance(report.get('assetId'), str) or not report['assetId'] or report.get('kind') not in ('static_mesh', 'material', 'native_files'):
        return False
    for key in ('name', 'description', 'category', 'path', 'reportPath'):
        if key in report and not isinstance(report[key], str):
            return False
    for key in ('inputs', 'maps', 'textureEvidence', 'thumbnail'):
        if key in report and not isinstance(report[key], dict):
            return False
    inputs = report.get('inputs', {})
    if 'versions' in inputs and not isinstance(inputs['versions'], dict):
        return False
    if 'source' in report and not isinstance(report['source'], (dict, str)):
        return False
    size = report.get('size')
    if size is not None and (not isinstance(size, list) or len(size) != 3 or any(
            type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in size)):
        return False
    return True


def _register_row(connection, report, versions):
    if not _report_metadata_valid(report):
        return
    identifier = report.get('assetId')
    reference = report.get('reportPath')
    if not identifier or not reference or report.get('kind') not in ('static_mesh', 'material', 'native_files'):
        return
    existing = connection.execute('SELECT payload FROM assets WHERE id=?', (identifier,)).fetchone()
    row = json.loads(existing[0]) if existing else None
    if row is None and not identifier.startswith(('generated:', 'imported:')):
        return
    references = row.get('preparationReports', []) if row else []
    if reference not in references:
        references = [*references, reference]
    if row is None:
        row = _prepared_row(report, versions)
    if row.get('prepared'):
        # Candidate labels improve recall, but cannot replace previously indexed semantics.
        texts = row.get('preparationSearchText', [])
        for key in ('name', 'description', 'category'):
            value = report.get(key)
            if value and value not in texts:
                texts = [*texts, value]
        row = {**row, 'preparationSearchText': texts}
    # Native catalog semantics and source paths remain canonical; only references are attached.
    _insert(connection, {**row, 'preparationReports': references})
    previous = connection.execute("SELECT value FROM metadata WHERE key='prepared_ids'").fetchone()
    identifiers = json.loads(previous[0]) if previous else []
    if identifier not in identifiers:
        connection.execute("INSERT OR REPLACE INTO metadata VALUES ('prepared_ids', ?)",
                           (json.dumps([*identifiers, identifier]),))


def _sync_prepared(connection, root, versions):
    prepared = root / 'assets/prepared'
    paths = sorted([*prepared.glob('*/report.json'),
                    *(path for path in prepared.glob('*/*/report.json')
                      if re.fullmatch('[a-f0-9]{64}', path.parent.name)
                      and re.fullmatch('[a-f0-9]{64}', path.parent.parent.name))],
                   key=lambda path: path.stat().st_mtime_ns)
    signature = _json_hash([(str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths])
    previous = connection.execute("SELECT value FROM metadata WHERE key='prepared'").fetchone()
    if previous and previous[0] == signature:
        return
    with connection:
        previous_ids = connection.execute("SELECT value FROM metadata WHERE key='prepared_ids'").fetchone()
        for identifier in json.loads(previous_ids[0]) if previous_ids else []:
            existing = connection.execute('SELECT rowid,payload FROM assets WHERE id=?', (identifier,)).fetchone()
            if not existing:
                continue
            row = json.loads(existing[1])
            if row.get('prepared'):
                connection.execute('DELETE FROM search WHERE rowid=?', (existing[0],))
                connection.execute('DELETE FROM assets WHERE id=?', (identifier,))
            else:
                row.pop('preparationReports', None)
                _insert(connection, row)
        connection.execute("DELETE FROM metadata WHERE key='prepared_ids'")
        for path in paths:
            report = _read_report(path)
            if report:
                _register_row(connection, {**report, 'reportPath': str(path)}, versions)
        connection.execute("INSERT OR REPLACE INTO metadata VALUES ('prepared', ?)", (signature,))


def _register(root, report):
    # The index holds disposable references; immutable reports remain the evidence authority.
    path = root / 'library/assets-v3.sqlite'
    if path.exists():
        with sqlite3.connect(path, timeout=60) as connection:
            _index_schema(connection)
            _register_row(connection, report, _versions())


def _glb_document(content):
    if len(content) < 20:
        raise ValueError('invalid GLB header')
    magic, version, size, chunk_length, chunk_type = struct.unpack_from('<4sIIII', content)
    if magic != b'glTF' or version != 2 or size != len(content) or chunk_type != 0x4E4F534A or chunk_length + 20 > size:
        raise ValueError('invalid GLB header or length')
    return json.loads(content[20:20 + chunk_length])


def _validate_gltf_materials(document, *, all_samplers=True):
    """Preserve preparation's whole-file and the compiler's referenced-sampler scope."""
    samplers = list(document.get('samplers', [])) if all_samplers else []
    for material in document.get('materials', []):
        extensions = material.get('extensions', {})
        if set(extensions) - {'KHR_materials_emissive_strength'}:
            raise ValueError('unsupported source material extensions')
        if extensions.get('KHR_materials_emissive_strength', {}).get('emissiveStrength', 1) != 1:
            raise ValueError('nondefault source emission strength is unsupported')
        textures = {**material.get('pbrMetallicRoughness', {}), **material}
        for name in ('baseColorTexture', 'normalTexture', 'metallicRoughnessTexture', 'emissiveTexture', 'occlusionTexture'):
            texture = textures.get(name, {})
            if (texture.get('scale', 1) != 1 or texture.get('strength', 1) != 1
                    or texture.get('texCoord', 0) != 0 or texture.get('extensions')):
                raise ValueError('nondefault source texture mapping is unsupported')
            if texture and not all_samplers:
                item = document['textures'][texture['index']]
                if 'sampler' in item:
                    samplers.append(document['samplers'][item['sampler']])
    for sampler in samplers:
        if any(sampler.get(axis, 10497) != 10497 for axis in ('wrapS', 'wrapT')):
            raise ValueError('nondefault source sampler wrap is unsupported')


def _texture_evidence(item):
    if item.get('format') == 'native_files':
        return {'status': 'not_inspected', 'imageCount': 0, 'uvVerified': False}
    if item.get('prepared'):
        return item.get('textureEvidence', {})
    path = Path(item['path'])
    if item['kind'] == 'material' or path.name.endswith('.pkl.gz'):
        maps = item.get('maps', item.get('materialInfo', {}))
        files = [Path(value) for key, value in maps.items() if key in ('albedo', 'albedo_path', 'normal', 'normal_path')]
        existing = [str(value) for value in files if value.is_file()]
        return {'status': 'source_files_present' if existing else 'none', 'maps': existing,
                'imageCount': len(existing), 'uvVerified': False}
    if path.suffix.lower() in ('.glb', '.gltf') and path.is_file():
        try:
            if path.suffix.lower() == '.glb':
                with path.open('rb') as file:
                    header = file.read(20)
                    length = struct.unpack_from('<I', header, 12)[0]
                    if length > 8 * 1024 * 1024:
                        raise ValueError('oversized GLB JSON')
                    document = json.loads(file.read(length))
            else:
                document = json.loads(path.read_text())
            materials = document.get('materials', [])
            return {'status': 'embedded_materials' if document.get('images') else 'none',
                    'imageCount': len(document.get('images', [])), 'materialCount': len(materials),
                    'baseColorMaps': sum('baseColorTexture' in m.get('pbrMetallicRoughness', {}) for m in materials),
                    'normalMaps': sum('normalTexture' in m for m in materials),
                    'uvVerified': False}
        except (ValueError, OSError, struct.error):
            return {'status': 'unreadable', 'imageCount': 0}
    return {'status': 'not_inspected', 'imageCount': 0}


def _size_fit(row, envelope):
    axis = row.get('sizeAxis') or row.get('upAxis')
    measured = _dimensions(row.get('size'))
    if not measured or row.get('sizeUnits', row.get('units')) != 'meters' or axis not in ('Y', 'Z'):
        return {'status': 'unknown'}
    if axis == 'Y':
        measured = [measured[0], measured[2], measured[1]]
    ratios = [float(value) / float(bound) for value, bound in zip(measured, envelope)]
    return {'status': 'fits' if max(ratios) <= 1 else 'exceeds', 'size': measured,
            'axis': 'Z', 'units': 'meters', 'envelope': list(envelope), 'largestRatio': max(ratios)}


def _asset_identifiers(asset_id):
    identifiers = [asset_id]
    if asset_id.startswith('objaverse:'):
        identifiers.append('objathor:' + asset_id.split(':', 1)[1])
    if ':' not in asset_id:
        identifiers.append('objathor:' + asset_id)
    return identifiers


def _source_item(legacy, root, asset_id):
    identifiers = _asset_identifiers(asset_id)
    with contextlib.closing(_index(legacy, root)) as connection:
        result = connection.execute('SELECT payload FROM assets WHERE id IN (' + ','.join('?' for _ in identifiers) + ')', identifiers).fetchone()
    if result is None:
        return None
    item = json.loads(result[0])
    if item.get('prepared'):
        references = item.get('preparationReports', [])
        versions = _versions()
        fallback = None
        # Explicit old generated/online IDs keep their frozen source for offline re-preparation.
        for reference in reversed(references):
            report = _cache_valid(Path(reference).parent)
            if report and report['assetId'] == item['id']:
                if fallback is None:
                    fallback = report
                if report.get('inputs', {}).get('versions') == versions:
                    item = _prepared_row(report, versions)
                    break
        else:
            if fallback is not None:
                item = _prepared_row(fallback, versions)
    item.pop('preparationReports', None)
    return item


def _source_dependencies(legacy, item):
    if item.get('format') == 'native_files' and item.get('collection'):
        package = _collection_path(Path(item['sourceRootPath']), item['packageRelative'])
        paths = [_collection_path(package, name) for name in [item['entry'], *item['dependencyFiles']]]
        if any(not path.is_file() for path in paths):
            raise ValueError('collection native dependency is missing or not a plain file')
        return paths
    if item['kind'] == 'material':
        return [Path(value) for value in item['maps'].values()]
    path = Path(item['path'])
    if path.suffix.lower() not in ('.glb', '.gltf', '.obj', '.ply', '.stl') and not path.name.endswith('.pkl.gz'):
        raise ValueError('static meshes only; articulated SDF conversion is not implemented')
    # Curated Objaverse entries are symlinks into the existing provider cache.
    allowed = legacy
    if not path.resolve().is_relative_to(legacy.resolve()):
        if item.get('source') == 'objathor' and item.get('index') == SOURCE_INDICES[0]:
            allowed = (legacy / 'data/objathor-assets/assets').resolve()
        elif item.get('index') == 'assets/objaverse/index.json':
            allowed = path.resolve().parent
        else:
            raise ValueError('indexed asset escapes the declared source library')
    return dependency_paths(path, allowed_root=allowed)


def _words_match(row, words):
    tokens = set(_query(' '.join(str(row.get(key, '')) for key in ('name', 'description', 'category', 'aliases'))).split())
    return any(word in tokens or word == 'wood' and 'wooden' in tokens for word in words)


def _preparation_evidence(legacy, row, versions, words=()):
    references = row.pop('preparationReports', [])
    row.pop('preparationSearchText', None)
    source_item = dict(row)
    dependencies = None
    status = 'not_prepared'
    for reference in reversed(references):
        metadata = _read_report(Path(reference))
        if metadata is None or not _report_metadata_valid(metadata) or metadata.get('assetId') != row['id']:
            status = 'invalid'
            continue
        if metadata.get('inputs', {}).get('versions') != versions:
            status = 'stale'
            continue
        report = _cache_valid(Path(reference).parent)
        if report is None:
            status = 'invalid'
            continue
        if not row.get('prepared'):
            try:
                if dependencies is None:
                    dependencies = _dependencies(_source_dependencies(legacy, source_item))
            except (OSError, ValueError):
                status = 'source_changed'
                continue
            inputs = report['inputs']
            if inputs.get('dependencies') != dependencies:
                status = 'source_changed'
                continue
            if row['kind'] == 'material' and row.get('format') != 'native_files':
                matches = inputs.get('maps') == row['maps'] and all(
                    report.get(key) == row.get(key) for key in ('name', 'description', 'category', 'license'))
            else:
                original = inputs.get('sourceMetadata', {})
                matches = all(original.get(key) == value for key, value in source_item.items())
            if not matches:
                status = 'source_changed'
                continue
        prepared = _prepared_row(report, versions)
        if not row.get('prepared'):
            prepared.update({key: row[key] for key in ('name', 'description', 'category', 'aliases', 'kind') if key in row})
        result = {**row, **prepared, 'preparationStatus': 'current'}
        if row.get('prepared') and words and not _words_match(result, words):
            # Historical names are aliases only when their actual same-ID snapshot still verifies.
            aliases = []
            for old_reference in references:
                metadata = _read_report(Path(old_reference))
                if metadata is None or not _report_metadata_valid(metadata) or not _words_match(metadata, words):
                    continue
                previous = _cache_valid(Path(old_reference).parent)
                if previous and previous['assetId'] == row['id']:
                    aliases.extend(previous.get(key, '') for key in ('name', 'description'))
            if aliases:
                result['aliases'] = list(dict.fromkeys(aliases))
        return result
    if row.get('prepared'):
        # Report-only IDs retain the latest valid historical identity, without claiming current readiness.
        for reference in reversed(references):
            previous = _cache_valid(Path(reference).parent)
            if previous and previous['assetId'] == row['id']:
                row = {**row, **_prepared_row(previous, versions)}
                break
        # Historical metadata remains discoverable, but is not current measurement or visual evidence.
        row.update(size=None, sizeEvidence='unknown', thumbnailPath=None,
                   textureEvidence={'status': 'not_verified', 'imageCount': 0, 'uvVerified': False})
    row.update(currentPreparation=False, preparationStatus=status)
    return row


def assets(legacy, query='', limit=24, root=None, asset_id=None, **filters):
    started = time.perf_counter()
    legacy, root = Path(legacy), Path(root) if root is not None else None
    query = _query(query)
    words = query.split()
    requested_category = _normalize(_category('', filters.get('category', '')))
    query_category = requested_category or _normalize(_category(query))
    versions = _versions()
    library_tier = filters.get('libraryTier', 'all')
    if library_tier not in ('preferred', 'broad', 'all'):
        raise ValueError('libraryTier must be preferred, broad or all')
    collection = filters.get('collection')
    if collection is not None and (not isinstance(collection, str) or not collection.strip()):
        raise ValueError('collection must be a nonempty exact collection identity')
    requires_joints = filters.get('requiresJoints', False)
    if type(requires_joints) is not bool:
        raise ValueError('requiresJoints must be a boolean')
    limit = max(0, min(int(limit), 100))
    part_query = bool(asset_id or set(words) & {'part', 'parts', 'component', 'components', 'door', 'handle', 'drawer'})
    ranking_hints = {}
    def ranking_row(row):
        # Small report metadata improves recall only; the normal frozen-byte check still decides readiness.
        if not row.get('preparationReports'):
            return row
        if row['id'] not in ranking_hints:
            hints = [row]
            for reference in reversed(row.get('preparationReports', [])):
                report = _read_report(Path(reference))
                if (report and _report_metadata_valid(report) and report.get('assetId') == row['id']
                        and report.get('inputs', {}).get('versions') == versions):
                    prepared = _prepared_row(report, versions)
                    keys = ['size', 'sizeAxis', 'sizeUnits', 'currentPreparation']
                    if row.get('prepared'):
                        keys.extend(('name', 'description', 'category', 'path'))
                    hints.append({**row, **{key: prepared[key] for key in keys}})
            # An unverified later report must not hide a still-valid older identity or measurement.
            ranking_hints[row['id']] = max(hints, key=lambda hint: (score(hint), Path(hint['path']).is_file()))
        return ranking_hints[row['id']]
    def fit_priority(row):
        fit = _size_fit(row, filters['size']) if filters.get('size') else {'status': 'unknown'}
        return ({'fits': 2, 'unknown': 1, 'exceeds': 0}[fit['status']],
                -fit['largestRatio'] if fit['status'] == 'exceeds' else 0)
    def score(row):
        text = _query(' '.join(str(row.get(key, '')) for key in ('description', 'name', 'aliases')))
        tokens = set(text.split())
        return (bool(query_category and _normalize(row['category']) == query_category), *fit_priority(row),
                part_query or row.get('objectRole') != 'part',
                row.get('libraryTier') == 'preferred',
                sum(word in tokens or word == 'wood' and 'wooden' in tokens for word in words)
                + (2 if query and query in text else 0), bool(row.get('currentPreparation')))
    clauses, params, ranked = [], [], False
    with contextlib.closing(_index(legacy, root, versions)) as connection:
        if asset_id:
            identifiers = _asset_identifiers(asset_id)
            clauses.append('a.id IN (' + ','.join('?' for _ in identifiers) + ')')
            params.extend(identifiers)
        elif words:
            variants = set(words)
            if 'wood' in variants:
                variants.add('wooden')
            expression = ' OR '.join('"' + word + '"' for word in sorted(variants))
            clauses.append('search MATCH ?')
            params.append(expression)
            ranked = True
        kind = filters.get('kind')
        if kind:
            clauses.append('a.kind=?')
            params.append(kind)
        if library_tier != 'all':
            clauses.append('a.library_tier=?')
            params.append(library_tier)
        if collection is not None:
            clauses.append('a.collection=?')
            params.append(collection)
        if requested_category and kind != 'material' and not asset_id:
            # Report-only candidates may have several historical classes; verify the selected class below.
            clauses.append("(a.category=? OR a.id LIKE 'generated:%' OR a.id LIKE 'imported:%')")
            params.append(requested_category)
        table = 'search JOIN assets a ON search.id=a.id' if ranked else 'assets a'
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        joint_filter = {'required': requires_joints, 'status': 'not_required'}
        if requires_joints:
            counts = dict(connection.execute('SELECT a.joint_state,count(*) FROM ' + table + where + ' GROUP BY a.joint_state', params))
            joint_filter.update(status='source_inspected_structure_only', knownMovable=counts.get('movable', 0),
                knownNonMovable=counts.get('none', 0), unknown=counts.get('unknown', 0),
                scope='matching_catalog_before_required_joint_filter')
            clauses.append("a.joint_state='movable'")
        total = connection.execute('SELECT count(*) FROM assets').fetchone()[0]
        ordering, order_params = [], []
        if query_category and kind != 'material':
            ordering.append('(a.category=?) DESC')
            order_params.append(query_category)
        if filters.get('size'):
            # Source rows already normalize valid dimensions; keep the broad scan inside SQLite.
            axis = "COALESCE(json_extract(a.payload,'$.sizeAxis'),json_extract(a.payload,'$.upAxis'))"
            dimensions = ["json_extract(a.payload,'$.size[0]')",
                "CASE WHEN " + axis + "='Y' THEN json_extract(a.payload,'$.size[2]') ELSE json_extract(a.payload,'$.size[1]') END",
                "CASE WHEN " + axis + "='Y' THEN json_extract(a.payload,'$.size[1]') ELSE json_extract(a.payload,'$.size[2]') END"]
            ordering.append("CASE WHEN COALESCE(json_extract(a.payload,'$.sizeUnits'),json_extract(a.payload,'$.units'))='meters'"
                + " AND " + axis + " IN ('Y','Z') AND json_array_length(a.payload,'$.size')=3 THEN CASE WHEN "
                + ' AND '.join(value + '<=?' for value in dimensions) + ' THEN 2 ELSE 0 END ELSE 1 END DESC')
            order_params.extend(filters['size'])
        if not part_query:
            ordering.append("(COALESCE(json_extract(a.payload,'$.objectRole'),'unknown')!='part') DESC")
        if ranked:
            ordering.append('bm25(search)')
        ordering.append('a.id')
        prepared_ids = connection.execute("SELECT value FROM metadata WHERE key='prepared_ids'").fetchone()
        rows, pools, matched = [], {}, 0
        # Keep each source tier visible before the metadata candidate bound.
        for tier in (('preferred', 'broad') if library_tier == 'all' else (library_tier,)):
            where = ' WHERE ' + ' AND '.join([*clauses, 'a.library_tier=?'])
            values = [*params, tier]
            count = connection.execute('SELECT count(*) FROM ' + table + where, values).fetchone()[0]
            candidates = [json.loads(row[0]) for row in connection.execute('SELECT a.payload FROM ' + table + where
                + ' ORDER BY ' + ','.join(ordering) + ' LIMIT 1200', [*values, *order_params])]
            if prepared_ids:
                # Prepared measurements/classes may improve over their canonical source row before the cap.
                prepared = [json.loads(row[0]) for row in connection.execute('SELECT a.payload FROM ' + table + where
                    + ' AND a.id IN (SELECT value FROM json_each(?))', [*values, prepared_ids[0]])]
                candidates = list({row['id']: row for row in [*candidates, *prepared]}.values())
                if len(candidates) > 1200:
                    candidates.sort(key=lambda row: score(ranking_row(row)), reverse=True)
                    candidates = candidates[:1200]
            pools[tier] = {'matched': count, 'loaded': len(candidates), 'truncated': count > len(candidates)}
            matched += count
            rows.extend(candidates)
    rows.sort(key=lambda row: score(ranking_row(row)), reverse=True)
    items, checked = [], 0
    batch = max(8, limit)
    while checked < len(rows) and limit:
        if len(items) >= limit and score(ranking_row(rows[checked])) <= sorted(
                (score(item) for item in items), reverse=True)[limit-1]:
            break
        candidates = rows[checked:checked+batch]
        checked += len(candidates)
        for candidate in candidates:
            if not asset_id and not Path(candidate['path']).is_file():
                hint = ranking_row(candidate)
                if hint is candidate or not Path(hint['path']).is_file():
                    continue
            row = _preparation_evidence(legacy, candidate, versions, words if not asset_id else ())
            if requested_category and kind != 'material' and _normalize(row['category']) != requested_category:
                continue
            if collection is not None and row.get('collection') != collection:
                continue
            if requires_joints and _joint_state(row) != 'movable':
                continue
            if words and not asset_id and row.get('prepared') and not _words_match(row, words):
                continue
            if not asset_id and not Path(row['path']).is_file():
                continue
            evidence = _texture_evidence(row)
            if filters.get('textured') is True and not evidence.get('imageCount', 0):
                continue
            row['textureEvidence'] = evidence
            items.append(row)
    items.sort(key=score, reverse=True)
    items = items[:limit]
    for row in items:
        row['exists'] = Path(row['path']).is_file()
        row.setdefault('libraryTier', 'broad')
        row['semanticMatch'] = {'query': query, 'matchedWords': [word for word in words if _words_match(row, [word])],
                                'categoryEvidence': 'source_description_terms', 'ranking': 'fts_words_and_object_class'}
        row['measuredSize'] = {'status': row.get('sizeEvidence', 'unknown'), 'value': row.get('size'),
                               'units': row.get('sizeUnits', row.get('units')), 'axis': row.get('sizeAxis', row.get('upAxis'))}
        expected_size = filters.get('size')
        if expected_size:
            row['sizeMatch'] = _size_fit(row, expected_size)
        row['filesFrozen'] = bool(row.get('prepared') and row.get('currentPreparation'))
        row['rigidReady'] = bool(row['filesFrozen'] and row.get('format') != 'native_files')
        row['simulationReady'] = row['rigidReady']
        row['visualReady'] = bool(row['rigidReady'] and row.get('thumbnail', {}).get('status') == 'rendered')
        if 'licenseEvidence' not in row:
            row['licenseEvidence'] = {'status': 'unknown' if row.get('license') == 'unknown' else 'source_index_declaration',
                                      'value': row.get('license', ''), 'sourceLabel': row.get('sourceLicenseLabel', '')}
        row.pop('dependencyFiles', None)
    return {'totalIndexed': total, 'catalogAssets': total, 'matched': matched, 'items': items,
            'candidatePools': pools, 'candidatesTruncated': any(pool['truncated'] for pool in pools.values()),
            'preparationChecks': {'checked': checked, 'candidateCount': len(rows), 'remaining': len(rows)-checked},
            'jointFilter': joint_filter,
            'elapsedMs': round((time.perf_counter() - started) * 1000, 3),
            'note': '文件冻结只核来源与字节；原生资源不会自动获得物理、控制或视觉验证资格。原生文件包的rigidReady/simulationReady/visualReady=false表示相应资格尚未核验，不代表资产不可用或视觉质量差；具体限制看importRequirements和实际错误。模型匹配、尺寸及机器人操作仍需实际构建和观察。'}


def _texture_reference(tokens):
    arities = {'-blendu': 1, '-blendv': 1, '-boost': 1, '-mm': 2, '-texres': 1, '-clamp': 1,
               '-bm': 1, '-imfchan': 1, '-type': 1, '-colorspace': 1}
    while tokens and tokens[0].startswith('-'):
        option = tokens.pop(0)
        if option in ('-o', '-s', '-t'):
            count = 0
            while tokens and count < 3:
                try:
                    float(tokens[0])
                except ValueError:
                    break
                tokens.pop(0)
                count += 1
        elif option in arities:
            del tokens[:arities[option]]
        else:
            raise ValueError('unsupported texture option: ' + option)
    if not tokens:
        raise ValueError('missing texture filename')
    return ' '.join(tokens)


def dependency_paths(mesh_path, *, allowed_root=None):
    """Resolve local inputs before a loader can fetch or silently drop dependencies."""
    from urllib.parse import unquote, urlparse
    path = Path(mesh_path).resolve()
    allowed = Path(allowed_root).resolve() if allowed_root else path.parent
    dependencies = {}
    def add(file):
        file = file.resolve()
        if not file.is_relative_to(allowed):
            raise ValueError('asset dependency escapes allowed root: ' + str(file))
        if not file.is_file():
            raise ValueError('asset dependency missing: ' + str(file))
        dependencies[str(file)] = file
        return file.read_bytes()
    def local(base, reference):
        uri = urlparse(reference)
        if uri.scheme or uri.netloc or uri.query or uri.fragment:
            raise ValueError('nonlocal asset dependency: ' + reference)
        if Path(unquote(reference)).is_absolute():
            raise ValueError('asset references must be relative to their frozen source')
        return base / unquote(reference)
    content = add(path)
    suffix = path.suffix.lower()
    if path.name.endswith('.pkl.gz'):
        for name in ('albedo.jpg', 'normal.jpg', 'emission.jpg'):
            add(path.parent / name)
    elif suffix == '.obj':
        for line in content.decode().splitlines():
            tokens = shlex.split(line, comments=True)
            if not tokens or tokens[0].lower() != 'mtllib':
                continue
            names = tokens[1:]
            if len(names) > 1 and (path.parent / ' '.join(names)).is_file():
                names = [' '.join(names)]
            for name in names:
                material = local(path.parent, name)
                for material_line in add(material).decode().splitlines():
                    fields = shlex.split(material_line, comments=True)
                    if fields and (fields[0].lower().startswith('map_') or fields[0].lower() in ('bump', 'norm', 'disp', 'decal')):
                        add(local(material.parent, _texture_reference(fields[1:])))
    elif suffix in ('.gltf', '.glb'):
        document = _glb_document(content) if suffix == '.glb' else json.loads(content)
        _validate_gltf_materials(document)
        for field in ('buffers', 'images'):
            for entry in document.get(field, []):
                reference = entry.get('uri')
                if reference and not reference.startswith('data:'):
                    add(local(path.parent, reference))
    elif suffix == '.json':
        report = json.loads(content)
        for name in report.get('dependencyFiles', []):
            add(local(path.parent, name))
    return list(dependencies.values())


def _dependencies(paths):
    return [{'path': str(path), 'sha256': _file_hash(path), 'bytes': path.stat().st_size} for path in paths]


def _cache_valid(directory):
    directory = Path(directory)
    path = directory / 'report.json'
    report = _read_report(path)
    if report is None or not _report_metadata_valid(report) or not isinstance(report.get('inputs'), dict):
        return None
    if (report.get('reportPath') != str(path) or report.get('digest') != directory.name
            or _json_hash(report.get('inputs')) != directory.name):
        return None
    digests = report.get('frozenDigests')
    dependencies, files = report.get('dependencyFiles'), report.get('preparedFiles')
    if not isinstance(digests, dict) or not digests or not isinstance(dependencies, list) or not isinstance(files, list):
        return None
    if any(not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
           for name in [*digests, *dependencies, *files]):
        return None
    if set(dependencies) != set(digests) or set(files) != {'report.json', *digests}:
        return None
    core = {'source.json'}
    if report.get('kind') == 'material':
        maps = report.get('maps')
        if not isinstance(maps, dict) or not maps.get('baseColor') or any(not isinstance(name, str) for name in maps.values()):
            return None
        core.update(maps.values())
    elif report.get('kind') == 'static_mesh':
        core.add('visual.glb')
        if report.get('path') != str(directory / 'visual.glb'):
            return None
    elif report.get('kind') == 'native_files':
        entry, native_files = report.get('entry'), report.get('nativeFiles')
        if (not isinstance(entry, str) or not isinstance(native_files, list) or not native_files
                or any(not isinstance(name, str) or not name.startswith('files/') for name in native_files)
                or len(set(native_files)) != len(native_files) or entry not in native_files
                or report.get('path') != str(directory / entry)):
            return None
        core.update(native_files)
    else:
        return None
    thumbnail = report.get('thumbnail', {})
    thumbnail_path = report.get('thumbnailPath')
    if thumbnail.get('status') == 'rendered' or thumbnail_path is not None:
        filename = thumbnail.get('path')
        if (not isinstance(filename, str) or Path(filename).is_absolute() or '..' in Path(filename).parts
                or thumbnail_path != str(directory / filename)):
            return None
        core.add(filename)
    if not core.issubset(digests):
        return None
    actual_files = {str(file.relative_to(directory)) for file in directory.rglob('*')
                    if file.is_file() and (str(file.relative_to(directory)) not in ('report.json', '.prepare.lock')
                       if report.get('kind') == 'native_files'
                       else file.name not in ('report.json', '.prepare.lock') and not file.name.endswith('.tmp'))}
    if actual_files != set(digests):
        return None
    for filename, digest in digests.items():
        candidate = directory / filename
        if not candidate.resolve().is_relative_to(directory.resolve()) or not candidate.is_file():
            return None
        try:
            if _file_hash(candidate) != digest:
                return None
        except OSError:
            return None
    if _read_report(directory / 'source.json') != report.get('source'):
        return None
    return report


def _freeze_inputs(directory, paths, expected=None):
    import os
    import shutil
    if not paths:
        return []
    common = Path(os.path.commonpath([str(path.parent) for path in paths]))
    frozen = []
    for path in paths:
        relative = Path('provenance/input') / path.relative_to(common)
        destination = directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        if expected is not None and _hash(destination.read_bytes()) != expected[str(path)]:
            raise ValueError('asset input changed while freezing: ' + str(path))
        frozen.append(str(relative))
    return frozen


def _restricted_prefab(path):
    import gzip
    import pickle
    import numpy as np
    class PrefabReader(pickle.Unpickler):
        def find_class(self, module, name):
            if module in ('numpy.core.multiarray', 'numpy._core.multiarray') and name == 'scalar':
                return np._core.multiarray.scalar
            if module == 'numpy' and name == 'dtype':
                return np.dtype
            raise ValueError('unsupported prefab pickle object: ' + module + '.' + name)
    return PrefabReader(io.BytesIO(gzip.decompress(path.read_bytes()))).load()


def _prefab_scene(path):
    import numpy as np
    import trimesh
    from PIL import Image
    payload = _restricted_prefab(path)
    if payload.get('action') != 'CreateObjectPrefab':
        raise ValueError('unrecognized Objathor coordinate format')
    def xyz(value):
        return [value[key] for key in ('x', 'y', 'z')] if isinstance(value, dict) else value
    def uv(value):
        return [value['x'], value['y']] if isinstance(value, dict) else value
    vertices = np.asarray([xyz(value) for value in payload['vertices']], dtype=float)
    triangles = np.asarray(payload['triangles'], dtype=int).reshape((-1, 3))
    normals = np.asarray([xyz(value) for value in payload['normals']], dtype=float)
    uvs = np.asarray([uv(value) for value in payload['uvs']], dtype=float)
    if uvs.shape != (len(vertices), 2) or not np.isfinite(vertices).all() or triangles.min() < 0 or triangles.max() >= len(vertices):
        raise ValueError('invalid prefab geometry or UV')
    uvs[:, 1] = 1 - uvs[:, 1]
    material = trimesh.visual.material.PBRMaterial(baseColorTexture=Image.open(path.parent / 'albedo.jpg').copy(),
               normalTexture=Image.open(path.parent / 'normal.jpg').copy(),
               emissiveTexture=Image.open(path.parent / 'emission.jpg').copy(), emissiveFactor=[0, 0, 0],
               metallicFactor=0, roughnessFactor=.7)
    geometry = trimesh.Trimesh(vertices=vertices, faces=triangles, vertex_normals=normals, process=False,
                             visual=trimesh.visual.TextureVisuals(uv=uvs, material=material))
    return trimesh.Scene(geometry)


def _image_stats(image, role, path=None):
    from PIL import ImageStat
    sample = ImageStat.Stat(image.convert('RGBA').resize((32, 32)))
    result = {'role': role, 'width': image.width, 'height': image.height, 'mode': image.mode,
              'channels': list(image.getbands()),
              'colorSpace': 'sRGB' if role in ('baseColor', 'baseColorTexture', 'emissive', 'emissiveTexture', 'image') else 'linear',
              'rgbaMean': sample.mean, 'rgbaExtrema': [list(value) for value in sample.extrema], 'pixelDigest': _hash(image.tobytes())}
    if role in ('metallicRoughness', 'metallicRoughnessTexture'):
        result['channelUsage'] = {'roughness': 'G', 'metallic': 'B'}
    elif role in ('occlusion', 'occlusionTexture'):
        result['channelUsage'] = {'occlusion': 'R'}
    if path is not None:
        result['sha256'] = _hash(Path(path).read_bytes())
    return result


def _red_channel(image):
    if image.mode == 'L':
        return image.copy()
    if image.mode in ('RGB', 'RGBA'):
        return image.getchannel('R')
    raise ValueError('scalar material maps require 8-bit L, RGB or RGBA data')


def _visual_stats(scene):
    materials, material_values, images, groups = {}, [], [], {}
    textured, vertex_count, face_count = 0, 0, 0
    for name, mesh in sorted(scene.geometry.items()):
        vertex_count += len(mesh.vertices)
        face_count += len(mesh.faces)
        visual = mesh.visual
        if visual.kind != 'texture':
            continue
        material = visual.material
        source_materials = getattr(material, 'materials', None)
        if source_materials is None:
            source_materials = [material]
            face_groups = {0: list(range(len(mesh.faces)))}
        else:
            indices = getattr(visual, 'face_materials', None)
            if indices is None or len(indices) != len(mesh.faces) or any(
                    not isinstance(index, Integral) or index < 0 or index >= len(source_materials) for index in indices):
                raise ValueError('multi-material mesh requires valid per-face indices')
            face_groups = {int(index): [i for i, value in enumerate(indices) if value == index] for index in sorted(set(indices))}
        groups[name] = []
        has_texture = False
        for source_index, faces in face_groups.items():
            current = source_materials[source_index]
            index = materials.get(id(current))
            if index is None:
                index = len(material_values)
                materials[id(current)] = index
                values = {'name': getattr(current, 'name', '')}
                for field in ('baseColorFactor', 'metallicFactor', 'roughnessFactor', 'emissiveFactor',
                              'alphaMode', 'alphaCutoff', 'doubleSided'):
                    value = getattr(current, field, None)
                    values[field] = value.tolist() if hasattr(value, 'tolist') else value
                material_values.append(values)
            groups[name].append({'materialIndex': index, 'faceIndices': faces})
            for role in ('baseColorTexture', 'normalTexture', 'metallicRoughnessTexture', 'emissiveTexture', 'occlusionTexture', 'image'):
                image = getattr(current, role, None)
                if image is not None:
                    has_texture = True
                    images.append({**_image_stats(image, role), 'materialIndex': index, 'geometry': name})
        if has_texture:
            uv = getattr(visual, 'uv', None)
            if uv is None or len(uv) != len(mesh.vertices) or any(
                    len(value) != 2 or not all(math.isfinite(float(component)) for component in value) for value in uv):
                raise ValueError('textured mesh has missing UV or invalid UV')
            textured += 1
    bindings = []
    graph = getattr(scene, 'graph', None)
    for node in sorted(graph.nodes_geometry if graph is not None else []):
        transform, geometry = graph[node]
        matrix = transform.tolist() if hasattr(transform, 'tolist') else transform
        for group in groups.get(geometry, []):
            bindings.append({'node': node, 'geometry': geometry, 'transform': matrix, **group})
    return {'status': 'prepared_texture_uv' if images else 'untextured', 'meshCount': len(scene.geometry),
            'materialCount': len(materials), 'imageCount': len({image['pixelDigest'] for image in images}),
            'imageReferenceCount': len(images), 'texturedMeshes': textured,
            'uvVerified': bool(textured), 'images': images, 'materials': material_values, 'bindings': bindings,
            'vertexCount': vertex_count, 'faceCount': face_count}


def _thumbnail(directory, up_axis='Y'):
    """Render the actual frozen mesh through the same RGB path as simulation."""
    try:
        from runtime.simulation import render_asset_thumbnail
        path = render_asset_thumbnail({'kind': 'asset', 'path': str(directory / 'visual.glb'),
                                       'up_axis': up_axis}, directory)
        return {'status': 'rendered', 'kind': 'asset_render', 'renderer': 'Isaac Sim 6.0.0.1 RTX / PhysX',
                'path': str(Path(path).relative_to(directory))}
    except (ImportError, ValueError, RuntimeError, OSError) as error:
        return {'status': 'failed', 'kind': 'asset_render', 'reason': str(error)}


def _publish_model(root, item, paths, cache_input, *, scene=None, source=None, source_files=None, material_reports=None):
    import fcntl
    import trimesh
    key = _json_hash(cache_input)
    directory = root / 'assets/prepared' / key
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.prepare.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cached = _cache_valid(directory)
        if cached:
            return cached
        if scene is None:
            if paths:
                expected = {entry['path']: entry['sha256'] for entry in cache_input['dependencies']}
                frozen = _freeze_inputs(directory, paths, expected)
                path = directory / frozen[0]
            else:
                path = directory / 'provenance/generated-output.glb'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(Path(item['path']).read_bytes())
            scene = _prefab_scene(path) if path.name.endswith('.pkl.gz') else trimesh.load(path, force='scene')
        if not scene.geometry:
            raise ValueError('asset has no mesh geometry')
        stats = _visual_stats(scene)
        bounds, extents = scene.bounds.tolist(), scene.extents.tolist()
        if not all(math.isfinite(value) and value > 0 for value in extents):
            raise ValueError('asset bounds must be finite nonzero in three dimensions')
        visual = scene.export(file_type='glb')
        output = directory / 'visual.glb'
        temporary = output.with_name('visual.' + uuid.uuid4().hex + '.tmp')
        temporary.write_bytes(visual)
        temporary.replace(output)
        if source_files:
            for name, content in source_files.items():
                destination = directory / 'provenance/source' / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content)
        if material_reports:
            import shutil
            for material in material_reports:
                material_path = Path(material['reportPath'])
                for path in dependency_paths(material_path):
                    destination = directory / 'provenance/materials' / material['digest'] / path.relative_to(material_path.parent)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
        source = source or {'kind': item['source'], 'path': item['path'], 'index': item.get('index'),
                            'sourceDataset': item.get('sourceDataset'), 'metadata': item.get('sourceMetadata', {}),
                            'licenseEvidence': {'status': 'unknown' if item.get('license') == 'unknown' else 'source_index_declaration',
                                                'value': item.get('license', ''), 'sourceLabel': item.get('sourceLicenseLabel', '')}}
        atomic_json(directory / 'source.json', source)
        thumbnail = _thumbnail(directory, item.get('upAxis', 'Z'))
        files = [str(path.relative_to(directory)) for path in directory.rglob('*') if path.is_file() and path.name not in ('report.json', '.prepare.lock') and not path.name.endswith('.tmp')]
        report = {'assetId': item['id'], 'name': item['name'], 'description': item.get('description', item['name']),
                  'category': item.get('category') or _category(item.get('name', '') + ' ' + item.get('description', '')),
                  'path': str(output), 'reportPath': str(directory / 'report.json'),
                  'digest': key, 'visualDigest': _hash(visual), 'bounds': bounds, 'extents': extents,
                  'size': [extents[0], extents[2], extents[1]] if item.get('upAxis') == 'Y' else extents,
                  'sizeAxis': 'Z', 'sizeUnits': item.get('units', 'unknown'),
                  'upAxis': item.get('upAxis', 'Z'), 'units': item.get('units', 'unknown'), 'kind': 'static_mesh',
                  'collisionFidelity': 'bounding_box', 'articulationPrepared': False, 'license': item.get('license', ''),
                  'source': source, 'inputs': cache_input, 'textureEvidence': stats,
                  'thumbnail': thumbnail, 'thumbnailPath': str(directory / 'thumbnail.png') if thumbnail['status'] == 'rendered' else None,
                  'dependencyFiles': files, 'frozenDigests': {name: _hash((directory / name).read_bytes()) for name in files},
                  'preparedFiles': ['report.json', *files],
                  'checks': ['scene_graph_loaded', 'measured_bounds', 'dependencies_frozen', 'materials_preserved']}
        atomic_json(directory / 'report.json', report)
        _register(root, report)
        return report


def _material_prepare(root, item):
    import fcntl
    import numpy as np
    from PIL import Image
    maps = item['maps']
    if not maps.get('albedo'):
        raise ValueError('material has no base color texture')
    paths = [Path(value) for value in maps.values()]
    for path in paths:
        if not path.is_file():
            raise ValueError('material texture missing: ' + str(path))
    cache_input = {'versions': _versions(), 'assetId': item['id'], 'maps': maps, 'dependencies': _dependencies(paths),
                   'factors': {'baseColor': [1, 1, 1, 1], 'metallic': 1 if 'metallic' in maps else 0,
                               'roughness': 1 if 'roughness' in maps else .7, 'emissive': [0, 0, 0]}}
    key = _json_hash(cache_input)
    directory = root / 'assets/prepared' / key
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.prepare.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cached = _cache_valid(directory)
        if cached:
            return cached
        frozen = _freeze_inputs(directory, paths, {entry['path']: entry['sha256'] for entry in cache_input['dependencies']})
        frozen_maps = {key: str(directory / frozen[index]) for index, key in enumerate(maps)}
        prepared_maps = {}
        for original, label in (('albedo', 'baseColor'), ('normal', 'normal'), ('ao', 'occlusion')):
            if maps.get(original):
                with Image.open(frozen_maps[original]) as image:
                    converted = (_red_channel(image) if original == 'ao' else image.convert(
                        'RGBA' if original == 'albedo' and 'A' in image.getbands() else 'RGB'))
                    converted.save(directory / (label + '.png'))
                prepared_maps[label] = label + '.png'
        if maps.get('roughness') or maps.get('metallic'):
            with Image.open(frozen_maps['albedo']) as base:
                size = base.size
            channels = np.full((size[1], size[0], 3), 255, dtype=np.uint8)
            channels[:, :, 2] = 0
            for channel, original in ((1, 'roughness'), (2, 'metallic')):
                if maps.get(original):
                    with Image.open(frozen_maps[original]) as image:
                        channels[:, :, channel] = np.asarray(_red_channel(image).resize(size))
            Image.fromarray(channels).save(directory / 'metallicRoughness.png')
            prepared_maps['metallicRoughness'] = 'metallicRoughness.png'
        with Image.open(directory / 'baseColor.png') as image:
            image.resize((320, 320)).save(directory / 'thumbnail.png')
        image_specs = []
        for role, filename in prepared_maps.items():
            with Image.open(directory / filename) as image:
                image_specs.append(_image_stats(image, role, directory / filename))
        source = {'kind': 'ambientcg', 'index': item['index'], 'licenseEvidence': {'status': 'source_index_declaration', 'value': item['license']}}
        atomic_json(directory / 'source.json', source)
        files = [str(path.relative_to(directory)) for path in directory.rglob('*') if path.is_file() and path.name not in ('.prepare.lock', 'report.json')]
        report = {'assetId': item['id'], 'name': item['name'], 'description': item['description'], 'category': item['category'],
                  'kind': 'material', 'reportPath': str(directory / 'report.json'), 'digest': key, 'source': source,
                  'license': item['license'], 'maps': prepared_maps, 'factors': cache_input['factors'], 'inputs': cache_input,
                  'alphaMode': 'OPAQUE', 'alphaCutoff': .5, 'doubleSided': False,
                  'textureEvidence': {'status': 'prepared_texture', 'imageCount': len(prepared_maps),
                                      'uvVerified': False, 'images': image_specs},
                  'thumbnail': {'status': 'rendered', 'kind': 'material_swatch', 'path': 'thumbnail.png'},
                  'thumbnailPath': str(directory / 'thumbnail.png'),
                  'dependencyFiles': files, 'frozenDigests': {name: _hash((directory / name).read_bytes()) for name in files},
                  'preparedFiles': ['report.json', *files],
                  **{label + 'Texture': filename for label, filename in prepared_maps.items()}}
        atomic_json(directory / 'report.json', report)
        _register(root, report)
        return report


def prepare(legacy, root, asset_id, *, project_id=None):
    legacy, root = Path(legacy), Path(root)
    item = _source_item(legacy, root, asset_id)
    if item is None or not Path(item['path']).is_file():
        raise ValueError('asset not found or source file missing')
    if item.get('prepared'):
        report = _cache_valid(Path(item['reportPath']).parent)
        if report is None:
            raise ValueError('prepared asset changed or missing')
        versions = _versions()
        if report.get('inputs', {}).get('versions') == versions:
            return report
        source = report['source']
        if report['kind'] == 'native_files':
            # Native files are already self-contained immutable inputs, not a mesh conversion.
            return report
        if source.get('kind') == 'online_import':
            retrieval = source['retrieval']
            digest = retrieval['sha256']
            if source != report['inputs'].get('source') or report['assetId'] != 'imported:' + digest:
                raise ValueError('online frozen source differs from its preparation inputs')
            previous = Path(report['reportPath']).parent
            names = [name for name, value in report['frozenDigests'].items()
                     if name.startswith('provenance/input/') and name.endswith('.glb') and value == digest]
            if len(names) != 1:
                raise ValueError('online frozen source GLB is missing or ambiguous')
            paths = [previous / names[0]]
            if paths[0].stat().st_size > 25 * 1024 * 1024:
                raise ValueError('online frozen source exceeds fixed size limit')
            _check_online_glb(paths[0].read_bytes())
            reference = source.get('importRef')
            if reference is not None:
                if reference.get('digest') != digest:
                    raise ValueError('online frozen source digest differs from importRef')
                receipt_digest = reference['retrievalDigest']
                receipts = [name for name, value in report['frozenDigests'].items()
                            if name.startswith('provenance/input/')
                            and Path(name).name == 'retrieval-' + receipt_digest + '.json' and value == receipt_digest]
                if len(receipts) != 1 or _read_report(previous / receipts[0]) != retrieval:
                    raise ValueError('online frozen retrieval receipt is missing or changed')
                paths.append(previous / receipts[0])
            cache_input = {**report['inputs'], 'versions': versions, 'dependencies': _dependencies(paths)}
            return _publish_model(root, {**item, 'description': report['description'], 'category': report['category']},
                                  paths, cache_input, source=source)
        if source.get('kind') != 'native_code_generated':
            raise ValueError('prepared asset has no supported frozen source for rebuilding')
        previous = Path(report['reportPath']).parent
        input_path = previous / 'provenance/generated-output.glb'
        if not input_path.is_file():
            raise ValueError('generated asset frozen output is missing')
        source_files = {name: (previous / 'provenance/source' / name).read_text()
                        for name in report['inputs']['sourceFiles']}
        material_reports = []
        for path in sorted((previous / 'provenance/materials').glob('*/report.json')):
            material_reports.append({**json.loads(path.read_text()), 'reportPath': str(path)})
        cache_input = {**report['inputs'], 'versions': versions, 'repreparedFrom': report['digest']}
        return _publish_model(root, {**item, 'path': str(input_path)}, [], cache_input,
                              source=source, source_files=source_files, material_reports=material_reports)
    if item.get('format') == 'native_files' and item.get('collection'):
        paths = _source_dependencies(legacy, item)
        cache_input = {'versions': _versions(), 'assetId': item['id'], 'dependencies': _dependencies(paths),
                       'sourceMetadata': item}
        source = {'kind': 'collection', 'collection': item['collection'], 'sourcePage': item['sourcePage'],
                  'revision': item['sourceRevision'], 'collectionRevision': item['collectionRevision'],
                  'licenseEvidence': item['licenseEvidence']}
        mdl_roots = item.get('mdlSearchRoots')
        if mdl_roots is not None:
            if not isinstance(mdl_roots, list): raise ValueError('MDL roots must be a list of relative directories')
            mdl_roots = [str(_collection_path(Path(item['packageRoot']), name)) for name in mdl_roots]
        elif any(path.suffix.lower() == '.mdl' for path in paths):
            mdl_roots = [item['packageRoot']]
        return prepare_native(root, paths[0], paths[1:], allowed_root=item['packageRoot'],
            source=source, name=item['name'], asset_id=item['id'], cache_input=cache_input, mdl_search_roots=mdl_roots)
    if item['kind'] == 'material':
        return _material_prepare(root, item)
    paths = _source_dependencies(legacy, item)
    cache_input = {'versions': _versions(), 'assetId': item['id'], 'dependencies': _dependencies(paths),
                   'upAxis': item['upAxis'], 'units': item['units'], 'sourceMetadata': item}
    return _publish_model(root, item, paths, cache_input)


def _generated_input(source_files, source_file, material_reports):
    if source_file not in source_files:
        raise ValueError('asset source file missing from frozen snapshot')
    for name in source_files:
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or path.suffix != '.py':
            raise ValueError('generated asset source must be a relative Python snapshot')
    materials = []
    for report in material_reports:
        path = Path(report['reportPath'])
        materials.append({'assetId': report['assetId'], 'dependencies': _dependencies(dependency_paths(path))})
    return {'versions': _versions(), 'sourceFile': source_file, 'sourceFiles': {name: _hash(content.encode()) for name, content in source_files.items()},
            'materials': materials, 'upAxis': 'Z', 'units': 'meters'}


def generated_cached_report(root, source_files, source_file, material_reports):
    cache_input = _generated_input(source_files, source_file, material_reports)
    native_input = {**cache_input, 'outputFormat': 'native_files'}
    prepared = Path(root) / 'assets/prepared'
    bucket = prepared / _json_hash({'nativeSourceInputs': native_input})
    candidates = [_cache_valid(prepared / _json_hash(cache_input)), _cache_valid(prepared / _json_hash(native_input))]
    for path in bucket.glob('*/report.json'):
        if path.parent.is_symlink() or not re.fullmatch('[a-f0-9]{64}', path.parent.name): continue
        metadata = _read_report(path)
        if (metadata and isinstance(metadata.get('inputs'), dict)
                and {key: value for key, value in metadata['inputs'].items()
                         if key not in ('nativeOutput', 'mdlSearchRoots')} == native_input):
            candidates.append(_cache_valid(path.parent))
    matches = [value for value in candidates if value is not None]
    # The source cannot select between differing output declarations without running its builder.
    return matches[0] if len(matches) == 1 else None


def prepare_local_native(legacy, root, entry_file, dependency_files=(), *, name='', license='', source_page='', project_id=None,
                         mdl_search_roots=None):
    """Local tool inputs may use public asset libraries and the pinned SDK, never projects."""
    legacy, root = Path(legacy).resolve(), Path(root).resolve()
    paths = [Path(entry_file), *map(Path, dependency_files)]
    libraries = [root / 'assets', legacy / 'assets', legacy / 'data', Path(sys.prefix).resolve()]
    # A redirected public-library directory must not grant access to another project or host tree.
    libraries = [path for path in libraries if path == Path(sys.prefix).resolve()
                 or path.resolve().is_relative_to(root if path.is_relative_to(root) else legacy)]
    common = next((path.resolve() for path in libraries
                   if all(value.is_absolute() and value.resolve().is_relative_to(path.resolve()) for value in paths)), None)
    if common is None:
        raise ValueError('native resource must belong to one public source root')
    source = {'kind': 'local_files', 'projectId': project_id, 'sourcePage': source_page,
              'sourcePaths': [str(value.resolve()) for value in paths],
              'licenseEvidence': {'status': 'caller_declaration_unverified' if license else 'unknown',
                                  'value': license or 'unknown'}}
    return prepare_native(root, paths[0], paths[1:], allowed_root=common, source=source, name=name, project_id=project_id,
                          mdl_search_roots=mdl_search_roots)


def _native_mdl_roots(allowed, paths, roots):
    if roots is None: return None
    if not isinstance(roots, list): raise ValueError('MDL roots must be a list of absolute directories')
    result = []
    for name in roots:
        if (not isinstance(name, str) or '\\' in name or not Path(name).is_absolute()
                or str(Path(name)) != name or '..' in Path(name).parts):
            raise ValueError('MDL roots must be normalized absolute directories')
        path = Path(name); resolved = path.resolve()
        if (not resolved.is_relative_to(allowed) or not resolved.is_dir()
                or not any(value.suffix.lower() == '.mdl' and value.is_relative_to(resolved) for value in paths)):
            raise ValueError('MDL root must be a directory containing declared MDL files in the allowed root')
        if any(value.is_symlink() and value.resolve().is_relative_to(allowed) for value in (path, *path.parents)):
            raise ValueError('MDL root cannot contain a symlink')
        relative = str(Path('files') / resolved.relative_to(allowed))
        if relative in result: raise ValueError('duplicate MDL root')
        result.append(relative)
    return result


def prepare_native(root, entry_file, dependency_files=(), *, allowed_root=None, source=None, name='', project_id=None,
                   source_files=None, material_reports=None, cache_input=None, asset_id=None, mdl_search_roots=None,
                   cache_scope=None):
    """Freeze declared native files without interpreting a robot, policy or physics recipe."""
    import fcntl
    import shutil
    root, entry = Path(root), Path(entry_file)
    allowed = Path(allowed_root).resolve() if allowed_root else entry.parent.resolve()
    paths, seen = [], set()
    for value in [entry, *map(Path, dependency_files)]:
        if value.is_symlink() or not value.is_file():
            raise ValueError('native resource must be a plain file, not a symlink')
        path = value.resolve()
        if not path.is_relative_to(allowed):
            raise ValueError('native resource escapes allowed root')
        if path in seen:
            raise ValueError('duplicate native resource path')
        seen.add(path)
        paths.append(path)
    entry = paths[0]
    names = {str(path): str(Path('files') / path.relative_to(allowed)) for path in paths}
    dependencies = _dependencies(paths)
    source = source or {'kind': 'local_files', 'projectId': project_id,
                        'licenseEvidence': {'status': 'unknown', 'value': 'unknown'}}
    inputs = dict(cache_input) if cache_input else {'versions': _versions(), 'entry': names[str(entry)], 'source': source,
                             'dependencies': [{**item, 'path': names[item['path']]} for item in dependencies]}
    mdl_roots = _native_mdl_roots(allowed, paths, mdl_search_roots)
    if mdl_roots is not None: inputs['mdlSearchRoots'] = mdl_roots
    key = _json_hash(inputs)
    if cache_scope is not None and not re.fullmatch('[a-f0-9]{64}', cache_scope):
        raise ValueError('invalid generated native cache scope')
    directory = root / 'assets/prepared'
    if cache_scope is not None: directory = directory / cache_scope
    directory = directory / key
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.prepare.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cached = _cache_valid(directory)
        if cached:
            return cached
        if (directory / 'report.json').exists():
            raise ValueError('native frozen resource package changed or missing')
        for item in dependencies:
            destination = directory / names[item['path']]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item['path'], destination)
            if _file_hash(destination) != item['sha256']:
                raise ValueError('native resource changed while freezing')
        for filename, content in (source_files or {}).items():
            destination = directory / 'provenance/source' / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        for material in material_reports or []:
            path = Path(material['reportPath'])
            for dependency in dependency_paths(path):
                destination = directory / 'provenance/materials' / material['digest'] / dependency.relative_to(path.parent)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(dependency, destination)
        atomic_json(directory / 'source.json', source)
        files = sorted(str(path.relative_to(directory)) for path in directory.rglob('*')
                       if path.is_file() and str(path.relative_to(directory)) not in ('report.json', '.prepare.lock'))
        generated = source_files is not None
        report = {'assetId': asset_id or ('generated:' if generated else 'imported:') + key, 'kind': 'native_files',
                  'name': name or entry.stem, 'description': name or entry.stem, 'category': '',
                  'entry': names[str(entry)], 'path': str(directory / names[str(entry)]),
                  'nativeFiles': list(names.values()), 'reportPath': str(directory / 'report.json'),
                  'digest': key, 'source': source, 'inputs': inputs,
                  'license': source.get('licenseEvidence', {}).get('value', 'unknown'),
                  'dependencyFiles': files, 'frozenDigests': {filename: _file_hash(directory / filename) for filename in files},
                  'preparedFiles': ['report.json', *files],
                  'checks': ['declared_files_frozen', 'file_digests_verified']}
        atomic_json(directory / 'report.json', report)
        _register(root, report)
        return report


def prepare_generated_native(root, output_directory, declaration, source_files, source_file, material_reports, *, name='', project_id=None):
    """Publish an isolated build_asset output declaration; no undeclared files survive."""
    output = Path(output_directory).resolve()
    if (not isinstance(declaration, dict) or not {'entry', 'files'}.issubset(declaration)
            or set(declaration) - {'entry', 'files', 'mdlSearchRoots'}):
        raise ValueError('native asset output requires entry and declared files')
    entry, names = declaration['entry'], declaration['files']
    if (not isinstance(entry, str) or not isinstance(names, list) or not names or entry not in names
            or any(not isinstance(value, str) or not value or Path(value).is_absolute()
                   or '..' in Path(value).parts or value != str(Path(value)) for value in names)
            or len(set(names)) != len(names)):
        raise ValueError('invalid native asset declared output paths')
    actual = set()
    for path in output.rglob('*'):
        if path.is_symlink():
            raise ValueError('native asset output cannot contain symlinks')
        if path.is_file():
            actual.add(str(path.relative_to(output)))
    if actual != set(names):
        raise ValueError('native asset declared files differ from actual output')
    inputs = {**_generated_input(source_files, source_file, material_reports), 'outputFormat': 'native_files'}
    cache_scope, mdl_roots = None, None
    if 'mdlSearchRoots' in declaration:
        roots = declaration['mdlSearchRoots']
        if (not isinstance(roots, list) or any(not isinstance(value, str) or not value or '\\' in value
                or Path(value).is_absolute() or '..' in Path(value).parts or str(Path(value)) != value for value in roots)):
            raise ValueError('generated MDL roots require normalized relative directories')
        mdl_roots = [str(output / value) for value in roots]
        cache_scope = _json_hash({'nativeSourceInputs': inputs})
        inputs['nativeOutput'] = declaration
    source = {'kind': 'native_code_generated', 'projectId': project_id, 'sourceFile': source_file,
              'sourceFiles': inputs['sourceFiles'], 'materials': inputs['materials'],
              'licenseEvidence': {'status': 'generated_source', 'value': 'generated-local'}}
    return prepare_native(root, output / entry, [output / filename for filename in names if filename != entry],
                          allowed_root=output, source=source, name=name, source_files=source_files,
                          material_reports=material_reports, cache_input=inputs, mdl_search_roots=mdl_roots,
                          cache_scope=cache_scope)


def prepare_generated(root, output_path, source_files, source_file, material_reports, *, name='', project_id=None):
    root, output_path = Path(root), Path(output_path)
    cache_input = _generated_input(source_files, source_file, material_reports)
    item = {'id': 'generated:' + _json_hash(cache_input), 'name': name or Path(source_file).stem,
            'source': 'native_code_generated', 'path': str(output_path), 'upAxis': 'Z', 'units': 'meters', 'license': 'generated-local'}
    dependency_paths(output_path, allowed_root=output_path.parent)
    source = {'kind': 'native_code_generated', 'projectId': project_id, 'sourceFile': source_file,
              'sourceFiles': cache_input['sourceFiles'], 'materials': cache_input['materials'],
              'licenseEvidence': {'status': 'generated_source', 'value': 'generated-local'}}
    return _publish_model(root, item, [], cache_input, source=source, source_files=source_files, material_reports=material_reports)


def _check_online_glb(content):
    document = _glb_document(content)
    _validate_gltf_materials(document)
    if not document.get('meshes'):
        raise ValueError('online GLB has no meshes')
    for field in ('buffers', 'images'):
        if any(row.get('uri') and not row['uri'].startswith('data:') for row in document.get(field, [])):
            raise ValueError('online GLB has external dependencies')


def _staged_online(root, url, reference):
    if not isinstance(reference, dict) or any(not isinstance(reference.get(key), str) or
            not re.fullmatch(r'[0-9a-f]{64}', reference[key]) for key in ('digest', 'retrievalDigest')):
        raise ValueError('invalid online import digest reference')
    imports = (root / 'assets/imports').resolve()
    if not imports.is_relative_to(root.resolve()):
        raise ValueError('online imports directory escapes the configured data root')
    directory = imports / reference['digest']
    source = directory / 'source.glb'
    receipt = directory / ('retrieval-' + reference['retrievalDigest'] + '.json')
    if any(not path.resolve().is_relative_to(imports) for path in (source, receipt)):
        raise ValueError('online import reference escapes imports directory')
    if source.stat().st_size > 25 * 1024 * 1024 or receipt.stat().st_size > 65536:
        raise ValueError('staged online import exceeds fixed size limit')
    content, metadata = source.read_bytes(), receipt.read_bytes()
    if _hash(content) != reference['digest'] or _hash(metadata) != reference['retrievalDigest']:
        raise ValueError('staged online import digest mismatch')
    retrieval = json.loads(metadata)
    if not isinstance(retrieval, dict) or retrieval.get('url') != url:
        raise ValueError('staged online import URL mismatch')
    if retrieval.get('sha256') != reference['digest'] or retrieval.get('bytes') != len(content) or retrieval.get('httpStatus') != 200:
        raise ValueError('staged online import receipt does not describe source bytes')
    _check_online_glb(content)
    return content, retrieval, [source, receipt]


def prepare_online(legacy, root, url, *, source_page='', name='', license='', project_id=None, import_ref=None):
    root = Path(root)
    if import_ref is None:
        raise ValueError('new online assets require a Mac-staged importRef; reprepare frozen assets by assetId')
    _, retrieval, staged_paths = _staged_online(root, url, import_ref)
    path = staged_paths[0]
    item = {'id': 'imported:' + retrieval['sha256'], 'name': name or Path(url).stem,
            'source': 'online_import', 'path': str(path), 'upAxis': 'Y', 'units': 'unknown', 'license': _license(license)}
    source = {'kind': 'online_import', 'projectId': project_id, 'retrieval': retrieval, 'sourcePage': source_page, 'importRef': import_ref,
              'licenseEvidence': {'status': 'caller_declaration_unverified' if license else 'unknown', 'value': _license(license)}}
    cache_input = {'versions': _versions(), 'dependencies': _dependencies(staged_paths), 'upAxis': 'Y', 'units': 'unknown',
                   'name': item['name'], 'source': source}
    return _publish_model(root, item, staged_paths, cache_input, source=source)
