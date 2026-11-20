import { spawn } from 'node:child_process';
import { readdir, readFile, open } from 'node:fs/promises';
import { constants } from 'node:fs';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { lookup } from 'node:dns/promises';
import { BlockList, isIP } from 'node:net';
import { ROOT, config, quote, validId } from './config.mjs';

const bootstrap = `import sys,json,base64,pathlib,io,runpy,os,fcntl,hashlib
packet=json.load(sys.stdin)
root=pathlib.Path(packet['payload']['root']).resolve()
code=root/'runtime'/packet['codeHash']
module=code/'runtime'
def missing():
    print(json.dumps({'type':'runtime_missing','codeHash':packet['codeHash']}),flush=True)
    sys.exit(75)
files=packet.get('files')
if files is None and not module.is_dir(): missing()
module.mkdir(parents=True,exist_ok=True)
with (code/'.deploy.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    digest=hashlib.sha256()
    for name in sorted(files if files is not None else packet['fileNames']):
        if pathlib.Path(name).name!=name: raise ValueError('invalid runtime filename')
        dest=module/name
        if files is None:
            if not dest.is_file(): missing()
            digest.update(name.encode());digest.update(dest.read_bytes())
            continue
        data=base64.b64decode(files[name])
        if not dest.exists():
            tmp=dest.with_name(dest.name+'.tmp-'+str(os.getpid()));tmp.write_bytes(data);os.replace(tmp,dest)
        elif dest.read_bytes()!=data: raise ValueError('runtime snapshot mismatch')
    if files is None and digest.hexdigest()!=packet['codeHash']: raise ValueError('runtime snapshot mismatch')
sys.path.insert(0,str(code))
sys.stdin=io.StringIO(json.dumps(packet['payload']))
runpy.run_path(str(module/'worker.py'),run_name='__main__')`;

function ssh(command) {
  if (!config.remoteHost || !config.remoteRoot || !config.remotePython) {
    throw new Error('请按 studio.config.example.json 配置仿真主机、数据目录和 Python 路径。');
  }
  return spawn('ssh', ['-C', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12', '-o', 'ServerAliveInterval=15',
    '-o', 'ControlMaster=auto', '-o', 'ControlPersist=60', '-o', 'ControlPath=~/.ssh/sceneagent-studio-%C',
    config.remoteHost, command], { stdio: ['pipe', 'pipe', 'pipe'] });
}

function singleJsonRemote(command, input, { timeoutMs, idleTimeoutMs, progressBytes, maxBytes, label, signal }) {
  if (signal?.aborted) return Promise.reject(signal.reason || new Error(label+'已取消'));
  return new Promise((resolve, reject) => {
    const child = ssh(command);
    const streaming = input && typeof input[Symbol.asyncIterator] === 'function';
    let chunks = [], bytes = 0, errors = Buffer.alloc(0), failure, closed = false, exited = false, exitCode, exitSignal,
      inputDone = !streaming, timer, killTimer, cleanupTimer, progressLine = '', discardProgressLine = false, received = 0;
    const finish = (code, unconfirmed = false) => {
      if (closed) return;
      if (!failure && !inputDone) return;
      closed = true;
      clearTimeout(timer); clearTimeout(killTimer); clearTimeout(cleanupTimer);
      signal?.removeEventListener('abort', abort);
      child.stdout.removeListener('data', output); child.stderr.removeListener('data', stderr);
      if (unconfirmed) {
        failure.message += '；SSH进程退出未确认';
        failure.exitUnconfirmed = true;
        child.stdin.destroy?.(); child.stdout.destroy?.(); child.stderr.destroy?.();
        child.unref?.();
      }
      if (failure || code !== 0) return reject(failure || new Error((errors.toString() || label + '失败')
        + '；SSH exitCode=' + String(code) + ', signal=' + String(exitSignal ?? null)));
      try { resolve(JSON.parse(Buffer.concat(chunks).toString())); }
      catch (error) { reject(error); }
    };
    const stop = error => {
      if (failure || closed) return;
      failure = error;
      if (streaming) input.destroy?.();
      child.stdin.destroy?.();
      child.stdout.destroy?.(); child.stderr.destroy?.();
      if (exited) { finish(exitCode); return; }
      killTimer = setTimeout(() => {
        cleanupTimer = setTimeout(() => finish(null, true), 1000);
        child.kill('SIGKILL');
      }, 1000);
      child.kill('SIGTERM');
    };
    const abort = () => stop(signal.reason || new Error(label+'已取消'));
    const output = chunk => {
      if (failure) return;
      const data = Buffer.from(chunk);
      bytes += data.length;
      if (bytes > maxBytes) { chunks = []; stop(new Error(label + '输出超过字节预算')); }
      else chunks.push(data);
    };
    const resetDeadline = () => {
      if (failure || closed) return;
      clearTimeout(timer);
      timer = setTimeout(() => stop(new Error(label + '超时')), idleTimeoutMs ?? timeoutMs);
    };
    const stderr = chunk => {
      errors = Buffer.concat([errors, Buffer.from(chunk)]).subarray(-3000);
      if (!idleTimeoutMs || failure || closed) return;
      for (const char of Buffer.from(chunk).toString('latin1')) {
        if (char === '\n') {
          const match = !discardProgressLine && /^TRACE_SYNC_PROGRESS ([1-9][0-9]{0,15})$/.exec(progressLine);
          if (match) {
            const count = Number(match[1]);
            if (Number.isSafeInteger(count) && count > received && count <= progressBytes) {
              received = count; resetDeadline();
            }
          }
          progressLine = ''; discardProgressLine = false;
        } else if (!discardProgressLine) {
          if (progressLine.length === 64) { progressLine = ''; discardProgressLine = true; }
          else progressLine += char;
        }
      }
    };
    resetDeadline();
    child.on('error', stop); child.stdin.on('error', stop); child.stdout.on('error', stop); child.stderr.on('error', stop);
    child.stdout.on('data', output); child.stderr.on('data', stderr);
    child.once('close', (code, terminationSignal) => {
      exited = true; exitCode = code; exitSignal = terminationSignal;
      if (streaming && !inputDone && !child.stdin.writableFinished) stop(new Error(label + '输入流尚未完成，SSH已退出'));
      finish(code);
    });
    signal?.addEventListener('abort', abort, { once:true });
    if (signal?.aborted) abort();
    if (failure) return;
    if (streaming) {
      pipeline(input, child.stdin).then(() => { inputDone = true; if (exited) finish(exitCode); }, stop);
    } else {
      try { child.stdin.end(input); }
      catch (error) { stop(error); }
    }
  });
}

const privateNetworks = new BlockList();
for (const [address, prefix] of [['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10],
  ['127.0.0.0', 8], ['169.254.0.0', 16], ['172.16.0.0', 12], ['192.0.0.0', 24],
  ['192.0.2.0', 24], ['192.168.0.0', 16], ['198.18.0.0', 15], ['198.51.100.0', 24],
  ['203.0.113.0', 24], ['224.0.0.0', 3]]) privateNetworks.addSubnet(address, prefix, 'ipv4');

function onlineAssetUrl(value) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.port && url.port !== '443'
    || isIP(url.hostname) || ['localhost', 'localhost.localdomain'].includes(url.hostname)) throw new Error('在线模型仅支持公开 HTTPS 域名和443端口');
  return url;
}

export function validateOnlineAssetTarget(value, addresses) {
  const url = onlineAssetUrl(value);
  const approved = address => isIP(address) === 4 && (!privateNetworks.check(address, 'ipv4')
    || url.hostname === 'raw.githubusercontent.com' && /^198\.(18|19)\./.test(address));
  if (!addresses.length || !addresses.every(approved)) throw new Error('在线模型地址必须解析到公网；仅已知公共CDN可使用本机TUN解析');
  return { url, address: addresses[0] };
}

function validateOnlineGlb(bytes) {
  if (bytes.length < 20 || bytes.readUInt32LE(0) !== 0x46546c67 || bytes.readUInt32LE(4) !== 2
    || bytes.readUInt32LE(8) !== bytes.length || bytes.readUInt32LE(16) !== 0x4e4f534a) throw new Error('下载文件不是完整GLB模型');
  const length = bytes.readUInt32LE(12);
  if (20 + length > bytes.length) throw new Error('GLB模型结构无效');
  const document = JSON.parse(bytes.subarray(20, 20 + length).toString());
  if (!Array.isArray(document.meshes) || !document.meshes.length) throw new Error('GLB模型没有网格');
  for (const field of ['buffers', 'images']) {
    if ((document[field] || []).some(item => item.uri && !item.uri.startsWith('data:'))) throw new Error('在线GLB包含未冻结的外部依赖');
  }
}

async function downloadOnlineAsset(value, { signal } = {}) {
  const started = Date.now(), maximum = 25 * 1024 * 1024;
  let current = value;
  for (let redirect = 0; redirect < 4; redirect++) {
    if (signal?.aborted) throw signal.reason || new Error('在线模型下载已取消');
    const parsed = onlineAssetUrl(current);
    const remainingMs = 30000 - (Date.now() - started);
    if (remainingMs <= 0) throw new Error('在线模型下载超过30秒');
    const addresses = await new Promise((resolve, reject) => {
      let settled = false;
      const finish = (error, result) => {
        if (settled) return;
        settled = true; clearTimeout(timer); signal?.removeEventListener('abort', abort);
        if (error) reject(error); else resolve(result);
      };
      const abort = () => finish(signal.reason || new Error('在线模型下载已取消'));
      const timer = setTimeout(() => finish(new Error('在线模型DNS查询超时')), remainingMs);
      signal?.addEventListener('abort', abort, { once:true });
      if (signal?.aborted) { abort(); return; }
      Promise.resolve().then(() => settled ? undefined : lookup(parsed.hostname, { all:true, family:4 }))
        .then(result => finish(null, result), error => finish(error));
    });
    if (signal?.aborted) throw signal.reason || new Error('在线模型下载已取消');
    const { url, address } = validateOnlineAssetTarget(current, addresses.map(row => row.address));
    const remaining = 30 - (Date.now() - started) / 1000;
    if (remaining <= 0) throw new Error('在线模型下载超过30秒');
    const response = await new Promise((resolve, reject) => {
      // Use the existing Mac system route. No personal curl config or unchecked redirects.
      const child = spawn('/usr/bin/curl', ['-q', '--silent', '--show-error', '--proto', '=https',
        '--noproxy', '*', '--max-time', String(remaining), '--connect-timeout', '10',
        '--max-filesize', String(maximum), '--dump-header', '/dev/fd/3',
        '--resolve', `${url.hostname}:443:${address}`, url.toString()], { stdio: ['ignore', 'pipe', 'pipe', 'pipe'] });
      const chunks = []; let bytes = 0, headerBytes = 0, headers = '', errors = '', failure, closed = false, killTimer, cleanupTimer;
      const finish = (code, unconfirmed = false) => {
        if (closed) return;
        closed = true;
        clearTimeout(timer); clearTimeout(killTimer); clearTimeout(cleanupTimer);
        signal?.removeEventListener('abort', abort);
        if (unconfirmed) {
          failure.message += '；下载进程退出未确认'; failure.exitUnconfirmed = true;
          child.stdout.destroy?.(); child.stderr.destroy?.(); child.stdio[3].destroy?.(); child.unref?.();
        }
        if (failure || code !== 0) { reject(failure || new Error(errors || '在线模型下载失败')); return; }
        try {
          const blocks = headers.trim().split(/\r?\n\r?\n/).filter(block => /^HTTP\/\d(?:\.\d)?\s+\d{3}(?:\s|$)/i.test(block));
          const lines = (blocks.at(-1) || '').split(/\r?\n/), status = Number(lines[0]?.split(/\s+/)[1]);
          if (!Number.isInteger(status) || status < 200) throw new Error('在线模型缺少最终HTTP响应');
          const fields = Object.fromEntries(lines.slice(1).filter(line => line.includes(':')).map(line => {
            const at = line.indexOf(':'); return [line.slice(0, at).toLowerCase(), line.slice(at + 1).trim()];
          }));
          resolve({ status, fields, bytes:Buffer.concat(chunks) });
        } catch (error) { reject(error); }
      };
      const stop = error => {
        if (failure || closed) return;
        failure = error; clearTimeout(timer);
        killTimer = setTimeout(() => {
          cleanupTimer = setTimeout(() => finish(null, true), 1000);
          child.kill('SIGKILL');
        }, 1000);
        child.kill('SIGTERM');
      };
      const abort = () => stop(signal.reason || new Error('在线模型下载已取消'));
      const timer = setTimeout(() => stop(new Error('在线模型下载超过30秒')), remaining * 1000);
      child.stdout.on('data', chunk => {
        if (failure || closed) return;
        bytes += chunk.length;
        if (bytes > maximum) { chunks.length = 0; stop(new Error('在线GLB超过25 MiB')); } else chunks.push(chunk);
      });
      child.stdio[3].on('data', chunk => {
        if (failure || closed) return;
        headerBytes += chunk.length;
        if (headerBytes > 65536) stop(new Error('在线响应头过大')); else headers += chunk;
      });
      child.stderr.on('data', chunk => { if (!failure && !closed) errors = (errors + chunk).slice(-2000); });
      for (const stream of [child, child.stdout, child.stderr, child.stdio[3]]) stream.on('error', stop);
      child.once('close', code => finish(code));
      signal?.addEventListener('abort', abort, { once:true });
      if (signal?.aborted) abort();
    });
    if (signal?.aborted) throw signal.reason || new Error('在线模型下载已取消');
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      if (redirect === 3 || !response.fields.location) throw new Error('在线模型重定向次数超限');
      current = new URL(response.fields.location, url).toString(); continue;
    }
    if (response.status !== 200) throw new Error(`在线模型HTTP状态 ${response.status}`);
    validateOnlineGlb(response.bytes);
    return { bytes:response.bytes, retrieval:{ url:value, finalUrl:url.toString(), httpStatus:200,
      contentType:response.fields['content-type'] || '', bytes:response.bytes.length,
      retrievedAt:new Date().toISOString(), sha256:createHash('sha256').update(response.bytes).digest('hex'),
      transport:'mac_system_https_to_4090' } };
  }
}

export async function stageOnlineAsset(url, { signal } = {}) {
  if (signal?.aborted) throw signal.reason || new Error('在线资产准备已取消');
  onlineAssetUrl(url);
  const cacheScript = `import sys,json,pathlib,hashlib
p=json.loads(sys.argv[1]);root=pathlib.Path(p['root'])/'assets'/'imports'
for receipt in sorted(root.glob('*/retrieval-*.json')):
    data=receipt.read_bytes();value=json.loads(data)
    source=receipt.parent/'source.glb';receipt_hash=hashlib.sha256(data).hexdigest()
    if value.get('url')==p['url'] and receipt.name=='retrieval-'+receipt_hash+'.json' and source.is_file():
        digest=hashlib.sha256(source.read_bytes()).hexdigest()
        if digest==receipt.parent.name==value.get('sha256'):
            print(json.dumps({'digest':digest,'retrievalDigest':receipt_hash}));break
else: print('null')`;
  const cached = await singleJsonRemote(quote(config.remotePython) + ' -c ' + quote(cacheScript) + ' '
    + quote(JSON.stringify({ root: config.remoteRoot, url })), undefined,
  { timeoutMs: 30000, maxBytes: 65536, label: '在线缓存查询', signal });
  if (signal?.aborted) throw signal.reason || new Error('在线资产准备已取消');
  if (cached) return cached;
  const { bytes, retrieval } = await downloadOnlineAsset(url, { signal });
  if (signal?.aborted) throw signal.reason || new Error('在线资产准备已取消');
  const receipt = Buffer.from(JSON.stringify(retrieval));
  const retrievalDigest = createHash('sha256').update(receipt).digest('hex');
  const script = `import sys,json,pathlib,hashlib,os,re
p=json.loads(sys.stdin.buffer.readline());root=pathlib.Path(p['root'])/'assets'/'imports'
if not re.fullmatch('[a-f0-9]{64}',p['digest']) or not re.fullmatch('[a-f0-9]{64}',p['retrievalDigest']): raise ValueError('invalid import digest')
dest=root/p['digest'];dest.mkdir(parents=True,exist_ok=True)
data=sys.stdin.buffer.read(25*1024*1024+1)
if len(data)>25*1024*1024 or hashlib.sha256(data).hexdigest()!=p['digest']: raise ValueError('import transfer differs')
receipt=p['receipt'].encode()
if hashlib.sha256(receipt).hexdigest()!=p['retrievalDigest']: raise ValueError('import receipt differs')
for name,content in [('source.glb',data),('retrieval-'+p['retrievalDigest']+'.json',receipt)]:
    file=dest/name
    if file.exists() and file.read_bytes()!=content: raise ValueError('immutable import differs')
    if not file.exists():
        temp=file.with_name(name+'.tmp-'+str(os.getpid()));temp.write_bytes(content);os.replace(temp,file)
print(json.dumps({'digest':p['digest'],'retrievalDigest':p['retrievalDigest']}))`;
  const input = Buffer.concat([Buffer.from(JSON.stringify({ root: config.remoteRoot, digest: retrieval.sha256, retrievalDigest,
    receipt: receipt.toString() }) + '\n'), bytes]);
  return singleJsonRemote(quote(config.remotePython) + ' -c ' + quote(script), input,
    { timeoutMs: 300000, maxBytes: 65536, label: '在线模型传输', signal });
}

async function runtimePacket(payload, executionDevice, executionGpuPeakMiB, executionAllowGpu3 = false) {
  const cpu = ['health','assets','inspect','job','check','read','list','write'].includes(payload.operation)
    || payload.operation === 'submit' && payload.task?.operation === 'check';
  const device = cpu ? undefined : executionDevice ?? config.remoteGpu;
  if (!cpu && (!device || !Number.isInteger(device.index) || device.index < 0 || !/^GPU-[a-f0-9-]{36}$/.test(device.uuid || '')))
    throw new Error('请在项目配置中锁定本次执行显卡的编号和UUID');
  const files = {};
  const hash = createHash('sha256');
  for (const name of (await readdir(path.join(ROOT, 'runtime'))).filter(name => name.endsWith('.py') || name === 'motion-dependencies.json').sort()) {
    const bytes = await readFile(path.join(ROOT, 'runtime', name));
    files[name] = bytes.toString('base64');
    hash.update(name).update(bytes);
  }
  const request = { ...payload };
  delete request.device;
  delete request.gpuPeakMiB;
  delete request.allowGpu3;
  if (payload.task) { request.task = { ...payload.task }; delete request.task.device; delete request.task.gpuPeakMiB; delete request.task.allowGpu3; }
  if (!cpu) { request.device = device; if (request.task) request.task.device = device; }
  const peak = executionGpuPeakMiB ?? config.remoteGpuPeakMiB;
  if (!cpu && typeof executionAllowGpu3 !== 'boolean') throw new Error('卡 3 授权选项须为布尔值');
  if (!cpu && executionAllowGpu3) {
    if (!executionDevice || device.index !== 3) throw new Error('卡 3 授权须显式指定本次执行显卡的编号和UUID');
    if (peak === undefined) throw new Error('卡 3 授权须声明正整数 MiB 显存峰值');
    request.allowGpu3 = true;
    if (request.task) request.task.allowGpu3 = true;
  }
  if (!cpu && peak !== undefined) {
    if (!Number.isSafeInteger(peak) || peak <= 0 || ![0, 1, 2, ...(executionAllowGpu3 ? [3] : [])].includes(device.index))
      throw new Error('共享显存作业只允许卡 0、1、2，并须声明正整数 MiB 峰值');
    request.gpuPeakMiB = peak;
    if (request.task) request.task.gpuPeakMiB = peak;
  }
  return { files, codeHash: hash.digest('hex'), payload: { ...request,
    root: config.remoteRoot, legacyRoot: config.legacyRoot || path.posix.join(config.remoteRoot, 'asset-library') } };
}

export async function executeRemote(payload, { onEvent = () => {}, timeoutMs = 600000, signal, executionDevice, executionGpuPeakMiB, executionAllowGpu3 = false } = {}) {
  const cancelled = () => new Error('本地请求已取消；远端作业状态未确认，请通过原作业查询');
  if (signal?.aborted) throw cancelled();
  const packet = await runtimePacket(payload, executionDevice, executionGpuPeakMiB, executionAllowGpu3);
  if (signal?.aborted) throw cancelled();
  const deadline = Date.now() + timeoutMs, missing = Symbol('runtime missing');
  const invoke = input => new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(cancelled());
    const child = ssh(quote(config.remotePython) + ' -u -c ' + quote(bootstrap));
    let buffer = '', stderr = '', result, failure, settled = false, exited = false, timer, killTimer, cleanupTimer;
    let cacheMissing = false, received = 0;
    const finish = (code, unconfirmed = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer); clearTimeout(killTimer); clearTimeout(cleanupTimer);
      signal?.removeEventListener('abort', abort);
      child.stdout.removeListener('data', output); child.stderr.removeListener('data', errors);
      if (!failure && code === 75 && cacheMissing && received === 1 && result === undefined) return resolve(missing);
      if (failure || code !== 0 || result === undefined) {
        failure ||= new Error(stderr.trim() || `仿真作业未返回结果（退出码 ${code}）`);
        failure.message += unconfirmed ? '；本地 SSH 退出未确认' : '；本地 SSH 退出已确认';
        if (!failure.message.includes('远端作业状态未确认')) failure.message += '；远端作业状态未确认，请通过原作业查询';
        if (unconfirmed) {
          failure.exitUnconfirmed = true;
          child.stdin.destroy?.(); child.stdout.destroy?.(); child.stderr.destroy?.(); child.unref?.();
        }
        reject(failure);
      } else resolve(result);
    };
    const stop = error => {
      if (failure || settled) return;
      failure = error;
      clearTimeout(timer);
      child.stdin.destroy?.();
      if (exited) return;
      killTimer = setTimeout(() => {
        cleanupTimer = setTimeout(() => finish(null, true), 1000);
        child.kill('SIGKILL');
      }, 1000);
      child.kill('SIGTERM');
    };
    const abort = () => stop(cancelled());
    child.stdout.setEncoding('utf8');
    const consume = line => {
      if (failure || settled || !line.trim()) return;
      let event;
      try { event = JSON.parse(line); }
      catch { event = { type: 'progress', stage: 'runtime', message: line.slice(0, 2000) }; }
      try {
        received++;
        if (event.type === 'runtime_missing') {
          if (input.files || received !== 1 || event.codeHash !== input.codeHash) throw new Error('运行内核缓存回执无效');
          cacheMissing = true;
          return;
        }
        if (cacheMissing) throw new Error('运行内核缓存回执之后出现了执行输出');
        if (event.type === 'result') result = event.result;
        if (event.type === 'error') stop(new Error(event.error || '远端执行失败'));
        onEvent(event);
      } catch (error) { stop(error); }
    };
    const output = text => {
      if (failure || settled) return;
      buffer += text;
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {
        consume(buffer.slice(0, end)); buffer = buffer.slice(end + 1);
      }
    };
    const errors = data => { if (!failure && !settled) stderr = (stderr + data).slice(-12000); };
    timer = setTimeout(() => stop(new Error('本地请求超时；远端作业状态未确认，请通过原作业查询')), Math.max(0, deadline - Date.now()));
    child.on('error', stop); child.stdin.on('error', stop); child.stdout.on('error', stop); child.stderr.on('error', stop);
    child.stdout.on('data', output); child.stderr.on('data', errors);
    child.once('close', code => {
      if (settled) return;
      exited = true;
      consume(buffer);
      finish(code);
    });
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    if (!failure) {
      try { child.stdin.end(JSON.stringify(input)); }
      catch (error) { stop(error); }
    }
  });
  const { files, ...reference } = packet;
  const result = await invoke({ ...reference, fileNames: Object.keys(files || {}) });
  if (result !== missing) return result;
  if (signal?.aborted) throw cancelled();
  if (Date.now() >= deadline) throw new Error('本地请求超时，运行内核尚未提交；请通过原作业查询');
  return invoke(packet);
}

export function validateArtifactPath(relative) {
  if (typeof relative !== 'string' || !relative || relative.startsWith('/') || relative.split('/').some(part => part === '..' || part === '.' || !part)) {
    throw new Error('产物路径无效');
  }
  return relative;
}

const fileScript = `import sys,json,pathlib
p=json.loads(sys.argv[1]);root=pathlib.Path(p['root']).resolve();f=(root/p['path']).resolve()
if not f.is_relative_to(root) or not f.is_file(): raise ValueError('artifact outside data root or missing')
if p.get('scope') and not f.is_relative_to(root/p['scope']): raise ValueError('artifact outside expected episode scope')
if p['op']=='stat': print(json.dumps({'size':f.stat().st_size}))
else:
    with f.open('rb') as s:
        s.seek(p.get('start',0));left=None if p.get('scope') else p.get('length',f.stat().st_size)
        while left is None or left>0:
            block=s.read(262144 if left is None else min(left,262144))
            if not block: break
            sys.stdout.buffer.write(block)
            if left is not None: left-=len(block)
        sys.stdout.buffer.flush()`;

export function openRemoteFile(relative, { start = 0, length, scope } = {}) {
  validateArtifactPath(relative);
  if (scope !== undefined) validateArtifactPath(scope);
  const payload = { root: config.remoteRoot, path: relative, op: 'read', start, ...(length === undefined ? {} : { length }), ...(scope ? { scope } : {}) };
  return ssh(quote(config.remotePython) + ' -u -c ' + quote(fileScript) + ' ' + quote(JSON.stringify(payload)));
}

export async function statRemoteFile(relative) {
  validateArtifactPath(relative);
  return singleJsonRemote(quote(config.remotePython) + ' -c ' + quote(fileScript) + ' '
    + quote(JSON.stringify({ root: config.remoteRoot, path: relative, op: 'stat' })), undefined,
  { timeoutMs: 30000, maxBytes: 65536, label: '产物读取' });
}

async function writeProjectFiles(projectId, files, immutable = false) {
  if (!validId(projectId)) throw new Error('项目标识无效');
  for (const relative of Object.keys(files)) validateArtifactPath(relative);
  const script = `import sys,json,pathlib,os,base64,hashlib
p=json.load(sys.stdin);root=(pathlib.Path(p['root'])/'projects'/p['projectId']).resolve();root.mkdir(parents=True,exist_ok=True)
hashes={}
for name,encoded in p['files'].items():
    dest=(root/name).resolve()
    if not dest.is_relative_to(root): raise ValueError('project write outside data root')
    data=base64.b64decode(encoded)
    if p['immutable'] and dest.exists() and dest.read_bytes()!=data: raise ValueError('immutable artifact differs')
    dest.parent.mkdir(parents=True,exist_ok=True)
    if not dest.exists() or dest.read_bytes()!=data:
        tmp=dest.with_name(dest.name+'.tmp-'+str(os.getpid()));tmp.write_bytes(data);os.replace(tmp,dest)
    hashes[name]=hashlib.sha256(data).hexdigest()
print(json.dumps({'saved':True,'hashes':hashes}))`;
  return singleJsonRemote(quote(config.remotePython) + ' -c ' + quote(script),
    JSON.stringify({ root: config.remoteRoot, projectId, files, immutable }),
    { timeoutMs: 300000, maxBytes: 8 * 1024 * 1024, label: '项目产物保存' });
}

export async function syncTrace(projectId, trace, archive = [], { signal } = {}) {
  if (!validId(projectId) || trace?.name !== 'generation-trace.jsonl' || !Array.isArray(archive)) throw new Error('轨迹同步描述无效');
  const files = [...archive, trace], names = new Set();
  for (const file of files) {
    validateArtifactPath(file.name);
    if (names.has(file.name) || typeof file.path !== 'string' || !path.isAbsolute(file.path)
      || !Number.isSafeInteger(file.bytes) || file.bytes < 0 || !/^[a-f0-9]{64}$/.test(file.sha256 || '')
      || !Number.isSafeInteger(file.identity?.dev) || !Number.isSafeInteger(file.identity?.ino)) throw new Error('轨迹同步文件身份无效');
    names.add(file.name);
  }
  const script = `import sys,json,pathlib,os,hashlib,tempfile,stat,signal,time
def interrupted(signum,frame): raise RuntimeError('trace transfer interrupted')
for signum in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT): signal.signal(signum,interrupted)
p=json.loads(sys.stdin.buffer.readline(65537))
base=pathlib.Path(p['root']).resolve();root=base/'projects'/p['projectId']
if not root.resolve().is_relative_to(base): raise ValueError('project write outside data root')
relative=pathlib.Path(p['name'])
if relative.is_absolute() or any(part in ('','.','..') for part in relative.parts): raise ValueError('invalid project path')
dest=root/relative
for directory in [root,*[root.joinpath(*relative.parts[:i]) for i in range(1,len(relative.parts))]]:
    if directory.is_symlink(): raise ValueError('project directory is a symlink')
    directory.mkdir(parents=True,exist_ok=True)
if dest.is_symlink(): raise ValueError('project file is a symlink')
fd,temporary=tempfile.mkstemp(prefix='.trace-sync-',dir=dest.parent)
try:
    digest=hashlib.sha256();remaining=p['bytes'];last_progress=None
    with os.fdopen(fd,'wb') as output:
        prefix=p.get('prefixBytes',0)
        if prefix:
            with os.fdopen(os.open(dest,os.O_RDONLY|os.O_NOFOLLOW),'rb') as source:
                info=os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size!=prefix: raise ValueError('trace prefix changed before transfer')
                for chunk in iter(lambda:source.read(65536),b''):
                    output.write(chunk);digest.update(chunk);remaining-=len(chunk)
            if digest.hexdigest()!=p.get('prefixSha256'): raise ValueError('trace prefix hash differs')
        while remaining:
            chunk=sys.stdin.buffer.read1(min(65536,remaining))
            if not chunk: raise ValueError('trace transfer truncated')
            output.write(chunk);digest.update(chunk);remaining-=len(chunk)
            now=time.monotonic()
            if last_progress is None or now-last_progress>=1 or not remaining:
                print('TRACE_SYNC_PROGRESS '+str(p['bytes']-remaining),file=sys.stderr,flush=True);last_progress=now
        if sys.stdin.buffer.read(1): raise ValueError('trace transfer exceeds fixed range')
        if digest.hexdigest()!=p['sha256']: raise ValueError('trace transfer hash differs')
        output.flush();os.fsync(output.fileno())
    if p['immutable'] and dest.exists():
        old=hashlib.sha256()
        with os.fdopen(os.open(dest,os.O_RDONLY|os.O_NOFOLLOW),'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode): raise ValueError('project artifact is not a regular file')
            for chunk in iter(lambda:source.read(65536),b''): old.update(chunk)
        if old.hexdigest()!=p['sha256']: raise ValueError('immutable artifact differs')
    else:
        os.replace(temporary,dest)
        directory_fd=os.open(dest.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    print(json.dumps({'saved':True,'hashes':{p['name']:p['sha256']}}))
finally:
    if os.path.exists(temporary): os.unlink(temporary)`;
  const hashes = {};
  for (const file of files) {
    let previous;
    if (file === trace) {
      const probe = `import sys,json,pathlib,os,stat,hashlib
p=json.load(sys.stdin);base=pathlib.Path(p['root']).resolve();project=base/'projects'/p['projectId'];target=project/'generation-trace.jsonl'
if not project.resolve().is_relative_to(base) or project.is_symlink() or target.is_symlink(): raise ValueError('trace prefix cannot leave its root or be a symlink')
result={}
if target.exists():
    with os.fdopen(os.open(target,os.O_RDONLY|os.O_NOFOLLOW),'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode): raise ValueError('trace prefix must be a regular file')
        if 0<info.st_size<=p['bytes']:
            digest=hashlib.sha256()
            for chunk in iter(lambda:source.read(65536),b''): digest.update(chunk)
            after=os.fstat(source.fileno())
            if (info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns): raise ValueError('trace prefix changed during verification')
            result={'bytes':info.st_size,'sha256':digest.hexdigest()}
print(json.dumps(result))`;
      previous = await singleJsonRemote(quote(config.remotePython) + ' -u -c ' + quote(probe),
        JSON.stringify({ root:config.remoteRoot, projectId, bytes:file.bytes }),
        { timeoutMs:300000, maxBytes:65536, label:'轨迹已有内容核对', signal });
    }
    const input = Readable.from((async function* () {
      const source = await open(file.path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        const info = await source.stat();
        if (!info.isFile() || info.dev !== file.identity.dev || info.ino !== file.identity.ino || info.size < file.bytes) throw new Error('轨迹源文件身份或固定范围已变化');
        let prefixBytes = 0;
        if (Number.isSafeInteger(previous?.bytes) && previous.bytes > 0 && previous.bytes <= file.bytes
          && /^[a-f0-9]{64}$/.test(previous.sha256 || '')) {
          const prefix = createHash('sha256');
          for await (const chunk of source.createReadStream({ start:0, end:previous.bytes - 1, autoClose:false })) prefix.update(chunk);
          if (prefix.digest('hex') === previous.sha256) prefixBytes = previous.bytes;
        }
        yield Buffer.from(JSON.stringify({ root: config.remoteRoot, projectId, name: file.name,
          bytes: file.bytes, sha256: file.sha256, immutable: file !== trace,
          ...(prefixBytes ? { prefixBytes, prefixSha256:previous.sha256 } : {}) }) + '\n');
        const digest = createHash('sha256'); let bytes = 0;
        if (file.bytes) for await (const chunk of source.createReadStream({ start: 0, end: file.bytes - 1, autoClose: false })) {
          const start = Math.max(0, prefixBytes - bytes);
          bytes += chunk.length; digest.update(chunk);
          if (start < chunk.length) yield chunk.subarray(start);
        }
        if (bytes !== file.bytes || digest.digest('hex') !== file.sha256) throw new Error('轨迹源文件固定范围摘要已变化');
      } finally { await source.close(); }
    })());
    const receipt = await singleJsonRemote(quote(config.remotePython) + ' -u -c ' + quote(script), input,
      { idleTimeoutMs: 300000, progressBytes: file.bytes, maxBytes: 65536, label: '轨迹文件传输', signal });
    if (receipt?.saved !== true || receipt.hashes?.[file.name] !== file.sha256 || Object.keys(receipt.hashes).length !== 1) throw new Error('轨迹文件传输回执摘要不匹配');
    hashes[file.name] = file.sha256;
  }
  return { saved: true, hashes };
}

export async function writeRemoteProjectFile(projectId, relative, value, { immutable = false } = {}) {
  const bytes = Buffer.isBuffer(value) || value instanceof Uint8Array ? Buffer.from(value) : Buffer.from(JSON.stringify(value, null, 2));
  return writeProjectFiles(projectId, { [relative]: bytes.toString('base64') }, immutable);
}

export async function consumeProducedArtifacts(project, pointer, { job, phase = 'prepare' }) {
  if (!validId(project.id)) throw new Error('产物接收项目无效');
  const source = await readFile(path.join(ROOT, 'examples/read_delivery.py'), 'utf8');
  const evaluator = await readFile(path.join(ROOT, 'examples/evaluate.py'), 'utf8');
  const binding = { projectId: project.id, generationId: job.generationId, requirementVersion: job.requirementVersion,
    requestHash: job.requestHash, sourceHash: job.sourceHash, revision: job.revision,
    ...(job.videoOnly ? { videoOnly: true } : {}),
    ...(job.environmentRefs ? { requiredOutputs: job.requiredOutputs, environmentRefs: job.environmentRefs } : {}) };
  const script = `import sys,json,pathlib,hashlib,os
p=json.load(sys.stdin)
os.environ.update(HF_HUB_OFFLINE='1',HF_DATASETS_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1',HF_HOME='/tmp/sceneagent-delivery-readers')
tools=pathlib.Path(p['root'])/'tools/delivery-readers-v1'
sys.path[:0]=[str(tools/'harbor-0.24.0-py3-none-any.whl'),str(tools/'robomimic-d309eaecc18acf4152a830a895a6984b8ac71b05')]
scope={'__name__':'trusted_delivery_reader'}
exec(compile(p['source'],'read_delivery.py','exec'),scope)
report=scope['consume'](pathlib.Path(p['root'])/'projects'/p['projectId'],p['pointer'],p['binding'],p['episodes'],
    hashlib.sha256(p['source'].encode()).hexdigest(),p['evaluator'],frozen_ref=p.get('frozenRef'))
print(json.dumps(report,ensure_ascii=False,allow_nan=False))`;
  return singleJsonRemote(quote(config.remoteRoot + '/envs/delivery-readers-v1/bin/python') + ' -I -B -u -c ' + quote(script),
    JSON.stringify({ root: config.remoteRoot, projectId: project.id, pointer, binding,
      episodes: job.reviewedEpisodes.map(({ episodeId, taskId, use, revision }) => ({ episodeId, taskId, use,
        ...(job.environmentRefs ? { revision } : {}) })), source, evaluator,
      frozenRef: phase === 'terminal' ? job.producedArtifactsConsumer?.frozenManifestRef : undefined }),
    { timeoutMs: (job.workerTimeoutSeconds ?? 600) * 1000, maxBytes: 64 * 1024 * 1024, label: '生成器产物接收' });
}

export async function consumeRemoteGeneration(projectId, generationId, consumerSource) {
  if (!validId(projectId) || !validId(generationId)) throw new Error('生成轨迹消费作用域无效');
  const script = `import sys,json,pathlib,hashlib,os
p=json.load(sys.stdin);scope={'__name__':'frozen_generation_consumer'}
exec(compile(p['source'],'read_generation.py','exec'),scope)
project=pathlib.Path(p['root'])/'projects'/p['projectId']
def publish(relative,data):
    dest=project/relative;dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists() and dest.read_bytes()!=data: raise ValueError('immutable generation consumer artifact differs')
    if not dest.exists():
        temp=dest.with_name(dest.name+'.tmp-'+str(os.getpid()));temp.write_bytes(data);os.replace(temp,dest)
trace_relative,claimed_trace_hash,trace_data=scope['freeze_trace'](project)
frozen=(project/trace_relative).resolve()
if pathlib.Path(trace_relative).is_absolute() or not frozen.is_relative_to(project.resolve()) or frozen==(project/'generation-trace.jsonl').resolve(): raise ValueError('generation frozen trace scope differs')
trace_hash=hashlib.sha256(frozen.read_bytes()).hexdigest()
if trace_hash!=claimed_trace_hash or trace_relative!='consumer-reports/traces/'+trace_hash+'.jsonl': raise ValueError('generation frozen trace hash differs')
report=scope['consume'](project,p['generationId'],trace_ref=trace_relative)
if report.get('schema')!='sceneagent-generation-consumer-v2' or report.get('version')!=2: raise ValueError('generation consumer schema differs')
if report.get('projectId')!=p['projectId'] or report.get('generationId')!=p['generationId'] or report.get('traceRef')!=trace_relative: raise ValueError('generation consumer scope differs')
if report.get('traceHash')!=trace_hash: raise ValueError('generation consumer trace hash differs')
consumer_digest=hashlib.sha256(p['source'].encode()).hexdigest()
report.update(projectId=p['projectId'],generationId=p['generationId'],traceHash=trace_hash,traceRef='projects/'+p['projectId']+'/'+trace_relative,consumerDigest=consumer_digest)
relative='consumer-reports/generation-'+p['generationId']+'-'+trace_hash[:16]+'-'+consumer_digest[:16]+'.json'
publish(relative,json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2).encode())
summary={k:report[k] for k in ('schema','version','passed','projectId','generationId','traceHash','traceRef','consumerDigest','sampleCount','editSampleCount','toolSampleCount','agentTrainingSampleCount','diagnosticSampleCount','coverage','issues','requestVersions','consumedSourceHashes') if k in report}
summary['artifactRef']='projects/'+p['projectId']+'/'+relative
print(json.dumps(summary,ensure_ascii=False,allow_nan=False))`;
  return singleJsonRemote(quote(config.remotePython) + ' -u -c ' + quote(script),
    JSON.stringify({ root: config.remoteRoot, projectId, generationId, source: consumerSource }),
    { timeoutMs: 300000, maxBytes: 8 * 1024 * 1024, label: '生成轨迹消费' });
}
