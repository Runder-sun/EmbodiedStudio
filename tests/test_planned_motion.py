"""SYSTEM_VERIFICATION_ONLY: pinned numerical dependency metadata and loaded byte identity.
Old host motion execution/Franka model freezer tests are retained in the before baseline.
"""
import ast
import builtins
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import sysconfig
import tempfile
import types
import unittest
from unittest.mock import Mock,patch
from runtime import motion
SCHEMA=motion.SCHEMA
SOABI=sysconfig.get_config_var('SOABI')
REQUIRED=('curobo','warp','torch',*[f'curobo.curobolib.{name}_cu' for name in ('geom','kinematics_fused','lbfgs_step','line_search','tensor_step')])

class IdentityContracts(unittest.TestCase):
 def fixture(self):
  root=Path(self.enterContext(tempfile.TemporaryDirectory())).resolve();prefix=root/'prefix';site=prefix/'lib/python3.12/site-packages'
  namespace=dict(vars(motion));namespace.update(SCHEMA=SCHEMA,__file__=str(root/'motion.py'),_loaded={})
  names={'_motion_file_stat','motion_runtime_identity','loaded_motion_identity'}
  nodes=[node for node in ast.parse(Path(motion.__file__).read_text()).body if isinstance(node,ast.FunctionDef) and node.name in names]
  exec(compile(ast.Module(body=nodes,type_ignores=[]),motion.__file__,'exec'),namespace)
  def file(path,data=b'CPU selected content'):
   path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);s=path.stat()
   return {'path':str(path),'bytes':s.st_size,'sha256':hashlib.sha256(data).hexdigest(),'device':s.st_dev,'inode':s.st_ino,'mtimeNs':s.st_mtime_ns,'ctimeNs':s.st_ctime_ns}
  modules={}
  for name in REQUIRED:
   path=(site/'isaacsim/extscache/omni.warp.core-1.13.0+lx64/warp/__init__.py' if name=='warp' else site.joinpath(*name.split('.')))
   if name in ('curobo','torch'):path=path/'__init__.py'
   elif name.startswith('curobo.curobolib'):path=path.with_suffix('.so')
   modules[name]=file(path)
  lib=file(site/'torch/lib/libtorch_cpu.so',b'CPU native library content')
  abi={'_GLIBCXX_USE_CXX11_ABI':True,'_PYBIND11_COMPILER_TYPE':None,'_PYBIND11_STDLIB':None,'_PYBIND11_BUILD_ABI':None}
  raw={'executable':str(prefix/'bin/python'),'prefix':str(prefix),'python':sys.version,'SOABI':SOABI,
   'distributions':{'isaacsim':'6.0.0.1','torch':'2.11.0+cu128','warp-lang':None,'numpy':'2.3.1','nvidia-curobo':'0.0.post1.dev1'},
   'modules':modules,'mappedNativeLibraries':{row['path']:{k:v for k,v in row.items() if k!='path'}
    for row in [lib,*[row for name,row in modules.items() if name.startswith('curobo.curobolib.')]]},'torchABI':abi,'torchCUDA':'12.8'}
  lock={'schema':'sceneagent-motion-dependencies-v1','backend':{'name':'cuRobo','commit':'ebb71702f3f70e767f40fd8e050674af0288abe8'},
   'verification':{'assessment':{'decision':'PASS_WITH_EXPLICIT_DEVICE_SCOPE'},'actualLoadedIdentity':raw},
   'sourceFiles':{row['path']:{k:v for k,v in row.items() if k!='path'} for name,row in modules.items() if not name.startswith('curobo.curobolib')}}
  (root/'motion-dependencies.json').write_text(json.dumps(lock))
  self.enterContext(patch.object(sysconfig,'get_config_var',return_value=SOABI));self.enterContext(patch.object(sys,'prefix',str(prefix)));self.enterContext(patch.object(sys,'executable',raw['executable']))
  self.enterContext(patch.object(importlib.metadata,'version',side_effect=lambda name:raw['distributions'][name] if raw['distributions'][name] is not None else (_ for _ in ()).throw(importlib.metadata.PackageNotFoundError(name))))
  loaded={name:types.SimpleNamespace(__file__=row['path']) for name,row in modules.items()}
  loaded['curobo'].__version__='0.0.post1.dev1';loaded['warp'].__version__='1.13.0'
  loaded['torch'].__version__='2.11.0+cu128';loaded['torch'].version=types.SimpleNamespace(cuda='12.8');loaded['torch']._C=types.SimpleNamespace(**abi)
  self.enterContext(patch.dict(sys.modules,loaded));self.enterContext(patch.object(sys,'platform','linux'))
  maps='\n'.join('0-1 r-xp 0000 00:00 1 '+name for name in raw['mappedNativeLibraries'])
  read_text=Path.read_text
  self.enterContext(patch.object(Path,'read_text',lambda p,*a,**k:maps if str(p)=='/proc/self/maps' else read_text(p,*a,**k)))
  return root,namespace,raw,lock,loaded
 def identity(self,namespace):
  result=namespace['motion_runtime_identity']();self.assertTrue(result['available'],result.get('missing'));return result
 def test_metadata_does_not_import_cuda_or_hash_native_content(self):
  _,ns,_,_,_=self.fixture();ns['digest']=Mock(side_effect=AssertionError('status hashed native content'))
  original=builtins.__import__
  def guarded(name,*a,**k):
   if name.split('.')[0] in ('curobo','warp','torch','isaacsim'):raise AssertionError('status imported CUDA/SDK')
   return original(name,*a,**k)
  with patch('builtins.__import__',side_effect=guarded):self.identity(ns)
 def test_same_content_touch_or_replacement_preserves_frozen_identity_and_can_reverify(self):
  for mutation in ('touch','replace'):
   with self.subTest(mutation=mutation):
    root,ns,_,_,loaded=self.fixture();identity=self.identity(ns)
    ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch'])
    path=Path(loaded['curobo'].__file__);before=path.stat();content=path.read_bytes()
    if mutation=='touch':os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns+1000000))
    else:
     temporary=path.with_suffix('.replacement');temporary.write_bytes(content);temporary.replace(path)
    ns['motion_runtime_identity'].cache_clear();current=ns['motion_runtime_identity']()
    try:
     self.assertTrue(current['available'],current.get('missing'));self.assertEqual(current,identity)
     self.assertTrue(ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch'])['contentHashesComputed'])
    finally:self.doCleanups()
 def test_wrong_prefix_or_missing_module_is_unavailable_without_import(self):
  root,ns,raw,lock,_=self.fixture();Path(raw['modules']['curobo']['path']).unlink()
  result=ns['motion_runtime_identity']();self.assertFalse(result['available']);self.assertTrue(result['missing'])
 def test_loaded_content_checked_once_and_post_check_file_change_is_rejected(self):
  _,ns,_,_,loaded=self.fixture();identity=self.identity(ns)
  ns['digest']=Mock(wraps=motion.digest)
  result=ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch']);count=ns['digest'].call_count
  self.assertGreater(count,0);self.assertTrue(result['contentHashesComputed'])
  ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch']);self.assertEqual(ns['digest'].call_count,count)
  path=Path(loaded['curobo'].__file__);content=path.read_bytes();path.write_bytes(bytes([content[0]^1])+content[1:])
  with self.assertRaisesRegex(ValueError,'changed|differ|identity'):ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch'])
 def test_missing_extension_and_foreign_warp_family_are_rejected(self):
  for mutation in ('missing','foreign','wrong_active_root'):
   with self.subTest(mutation=mutation):
    root,ns,_,_,loaded=self.fixture();identity=self.identity(ns)
    if mutation=='missing':sys.modules.pop('curobo.curobolib.geom_cu')
    elif mutation=='foreign':sys.modules['warp.foreign']=types.SimpleNamespace(__file__=str(root/'wrong-prefix/warp/foreign.py'))
    else:loaded['curobo']=types.SimpleNamespace(__file__=str(root/'cumotion.py'))
    try:
     with self.assertRaisesRegex(ValueError,'loaded|family|module'):ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch'])
    finally:self.doCleanups()
 def test_default_sdk_idle_extension_does_not_create_a_second_active_backend(self):
  root,ns,_,_,loaded=self.fixture();identity=self.identity(ns)
  with patch.dict(sys.modules,{'isaacsim.robot_motion.cumotion':types.SimpleNamespace(__file__=str(root/'idle-sdk.py'))}):
   self.assertTrue(ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch'])['contentHashesComputed'])
 def test_unrelated_deleted_mapping_is_allowed_but_selected_binary_deletion_is_not(self):
  for selected in (False,True):
   with self.subTest(selected=selected):
    _,ns,_,_,loaded=self.fixture();identity=self.identity(ns);read_text=Path.read_text
    def maps(path,*args,**kwargs):
     text=read_text(path,*args,**kwargs)
     if str(path)=='/proc/self/maps':
      if selected:text=text.replace(identity['binaries'][0],identity['binaries'][0]+' (deleted)')
      else:text+='\n0-1 rw-s 0000 00:00 1 /dev/shm/unrelated-render-buffer (deleted)'
     return text
    self.enterContext(patch.object(Path,'read_text',maps))
    try:
     if selected:
      with self.assertRaisesRegex(ValueError,'mapped|native|deleted'):
       ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch'])
     else:self.assertTrue(ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch'])['contentHashesComputed'])
    finally:self.doCleanups()
 def test_wrong_interpreter_prefix_is_unavailable(self):
  _,ns,_,_,_=self.fixture()
  with patch.object(sys,'prefix','/different/prefix'):
   result=ns['motion_runtime_identity']();self.assertFalse(result['available']);self.assertIn('prefix',' '.join(result['missing']))
 def test_unlocked_loaded_family_source_is_not_accepted_from_the_right_prefix(self):
  _,ns,_,_,loaded=self.fixture();identity=self.identity(ns)
  path=Path(loaded['warp'].__file__).parent/'unlocked.py';path.write_text('CPU source outside the approved manifest')
  with patch.dict(sys.modules,{'warp.unlocked':types.SimpleNamespace(__file__=str(path))}):
   with self.assertRaisesRegex(ValueError,'locked closure'):
    ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch'])
 def test_omitted_required_native_mapping_is_not_a_ready_dependency_selection(self):
  root,ns,raw,lock,_=self.fixture();raw['mappedNativeLibraries'].pop(raw['modules']['curobo.curobolib.geom_cu']['path'])
  (root/'motion-dependencies.json').write_text(json.dumps(lock));result=ns['motion_runtime_identity']()
  self.assertFalse(result['available']);self.assertIn('native module mapping',' '.join(result['missing']))
 def test_first_loaded_check_verifies_bytes_and_rejects_mixed_native_family(self):
  for mutation in ('hash','mapping','warp_version'):
   with self.subTest(mutation=mutation):
    root,ns,raw,lock,loaded=self.fixture()
    if mutation=='hash':
     key=next(name for name in raw['mappedNativeLibraries'] if name.endswith('libtorch_cpu.so'))
     raw['mappedNativeLibraries'][key]['sha256']='0'*64;(root/'motion-dependencies.json').write_text(json.dumps(lock))
    identity=self.identity(ns)
    if mutation=='mapping':
     read_text=Path.read_text
     self.enterContext(patch.object(Path,'read_text',lambda p,*a,**k:read_text(p,*a,**k)+('\n0-1 r-xp 0000 00:00 1 /different/prefix/warp/bin/warp.so' if str(p)=='/proc/self/maps' else '')))
    elif mutation=='warp_version':loaded['warp'].__version__='1.18.0'
    try:
     with self.assertRaisesRegex(ValueError,'bytes|mapped|version|family'):
      ns['loaded_motion_identity'](identity,loaded['curobo'],loaded['warp'],loaded['torch'])
    finally:self.doCleanups()


if __name__=="__main__":unittest.main()
