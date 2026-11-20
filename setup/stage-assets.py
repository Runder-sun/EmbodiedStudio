"""Fixed finite source acquisition; no publisher code is executed or live catalog changed."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import urllib.parse
import urllib.request
import zipfile


def relative(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or '\\' in value or any(x in ('', '.', '..') for x in value.split('/')):
        raise ValueError('invalid manifest path')
    if os.name=='nt' and any(any(c in part for c in ':*?<>|') or part.rstrip(' .')!=part or part.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]} for part in p.parts):
        raise ValueError('package filename is unsupported on Windows')
    return p


def safe_child(root, value):
    p = root.joinpath(*relative(value).parts)
    if any(x.is_symlink() for x in [root, *p.parents] if x == root or root in x.parents):
        raise ValueError('staging path contains symlink')
    if not p.resolve().is_relative_to(root.resolve()):
        raise ValueError('staging file escapes destination')
    if p.is_symlink():
        raise ValueError('staging file is a symlink')
    return p


def verify(file, row):
    digest = hashlib.sha256() if row.get('sha256') else hashlib.sha1(b'blob ' + str(row['size']).encode() + b'\0')
    size = 0
    with file.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(chunk)
            digest.update(chunk)
    if size != row['size'] or digest.hexdigest() != row.get('sha256', row.get('gitBlob')):
        raise ValueError('download size/digest differs: ' + row['path'])


def extract(archive, root, row):
    total = 0
    with zipfile.ZipFile(archive) as source:
        seen = set()
        for item in source.infolist():
            name = item.filename.rstrip('/')
            relative(name)
            mode = item.external_attr >> 16
            if name in seen or stat.S_IFMT(mode) not in (0, stat.S_IFDIR, stat.S_IFREG):
                raise ValueError('duplicate or special archive entry')
            seen.add(name)
            total += item.file_size
            if total > row['expandedBytes'] or len(seen) > row['entries']:
                raise ValueError('archive expansion exceeds fixed manifest')
        for item in source.infolist():
            dest = safe_child(root, item.filename.rstrip('/'))
            if item.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as stream, dest.open('xb') as output:
                remaining = item.file_size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('truncated archive member')
                    output.write(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    raise ValueError('oversized archive member')


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme!='https':
            raise ValueError('fixed asset source redirected outside HTTPS')
        return super().redirect_request(request,fp,code,msg,headers,newurl)


def main(q):
    urllib.request.install_opener(urllib.request.build_opener(HttpsRedirect()))
    root = Path(q['destination'])
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or root.resolve() != root:
        raise ValueError('staging root must be a plain absolute directory')
    needed = sum(x['size'] + x.get('expandedBytes', 0) for x in q['files'])
    if shutil.disk_usage(root).free < needed + 256 * 1024 * 1024:
        raise ValueError('insufficient disk for selected finite manifest')
    for row in q['files']:
        file = safe_child(root, row['path'])
        file.parent.mkdir(parents=True, exist_ok=True)
        part = file.with_name(file.name + '.partial')
        if part.exists() or file.exists():
            raise ValueError('staging destination is not empty; use a new explicit request')
        url = row.get('url') or 'https://huggingface.co/datasets/' + q['repository'] + '/resolve/' + q['revision'] + '/' + urllib.parse.quote(row['path'], safe='/') + '?download=true'
        if urllib.parse.urlsplit(url).scheme != 'https':
            raise ValueError('only HTTPS fixed manifest source is allowed')
        size = 0
        with urllib.request.urlopen(url, timeout=60) as source, part.open('xb') as output:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > row['size']:
                    raise ValueError('download exceeds fixed bytes')
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        verify(part, row)
        os.replace(part, file)
        if row.get('archive') == 'zip':
            extract(file, safe_child(root, row['extractTo']), row)
        print(json.dumps({'path': row['path'], 'bytes': size, 'verified': True}), flush=True)
    catalog = q.get('catalog')
    if catalog:
        with (root / 'staged-catalog.json').open('x') as file:
            json.dump(catalog, file, ensure_ascii=False, indent=2)
    with (root / 'staging-receipt.json').open('x') as file:
        json.dump({'schema':'sceneagent-asset-staging-v1','source':q['repository'],'revision':q['revision'],
                   'files':q['files'],'registered':False,'runtimeVerified':False}, file, indent=2)
    return {'staged':True,'registered':False,'runtimeVerified':False,'bytes':sum(x['size'] for x in q['files'])}


if __name__ == '__main__':
    print(json.dumps(main(json.loads(sys.argv[1]))))
