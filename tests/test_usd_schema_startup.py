"""Fixed vendor schema resources and the existing CPU-parent spawn boundary, without Kit."""
import ast
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]


def functions(filename,names,scope):
    source=ROOT/'runtime'/filename;tree=ast.parse(source.read_text())
    nodes=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.Assign)) and
           (isinstance(node,ast.FunctionDef) and node.name in names or isinstance(node,ast.Assign) and
            any(isinstance(target,ast.Name) and target.id in names for target in node.targets))]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),scope)
    return scope


class CpuSchemaStartupTests(unittest.TestCase):
    def enter(self, context):
        value=context.__enter__();self.addCleanup(context.__exit__,None,None,None);return value

    def fixture(self):
        temporary=self.enter(tempfile.TemporaryDirectory());prefix=Path(temporary)
        scope={'Path':Path,'sys':SimpleNamespace(prefix=str(prefix),version_info=(3,12)), 'os':os,'json':json,
            'VERSION':'6.0.0.1','importlib':SimpleNamespace(metadata=SimpleNamespace(version=lambda _: '6.0.0.1')),
            '_digest':lambda path:hashlib.sha256(Path(path).read_bytes()).hexdigest(), '__file__':str(ROOT/'runtime/simulation.py')}
        functions('simulation.py',{'_usd_schema_resources','_initialize_usd_schemas','_cpu_environment'},scope)
        self.assertIn('_initialize_usd_schemas',scope,'shared fixed schema initialization missing')
        registry=SimpleNamespace(plugins={},returned=[],definitionMissing=None)
        definitions={};types={};resources=scope['_usd_schema_resources']()
        for resource,name,locked in resources:
            resource.mkdir(parents=True)
            aliases={'RobotSchema':{'RobotSchemaIsaacRobotAPI':'IsaacRobotAPI','RobotSchemaIsaacLinkAPI':'IsaacLinkAPI','RobotSchemaIsaacJointAPI':'IsaacJointAPI'},
                     'newton':{'NewtonPhysicsSceneAPI':'NewtonSceneAPI'},
                     'omniSensor':{'OmniSensorOmniSensorAPI':'OmniSensorAPI'}}.get(name,{})
            info={'Types':{key:{'alias':{'UsdSchemaBase':value}} for key,value in aliases.items()}}
            metadata={'Plugins':[{'Info':info,'Name':name,'ResourcePath':'.','Type':'resource' if name=='RobotSchema' else 'library'}]}
            (resource/'plugInfo.json').write_text(json.dumps(metadata));(resource/'generatedSchema.usda').write_text('#usda 1.0\n')
            for key,value in aliases.items():types[key]=name;definitions[value]=object()
        original_digest=scope['_digest']
        # Real vendor digests are checked in the single ABI batch; use this finite fixture's bytes here.
        locked_digests={str(resource/filename):scope['_digest'](resource/filename)
            for resource,_,locked in resources if locked for filename in ('plugInfo.json','generatedSchema.usda')}
        expected_digests={str(resource/filename):digest for resource,_,locked in resources if locked
            for filename,digest in zip(('plugInfo.json','generatedSchema.usda'),locked)}
        scope['_digest']=lambda path: expected_digests.get(str(path),original_digest(path)) if (
            str(path) in locked_digests and original_digest(path)==locked_digests[str(path)]) else original_digest(path)
        def register(path):
            resource=Path(path).parent if Path(path).name=='plugInfo.json' else Path(path)
            metadata=json.loads((resource/'plugInfo.json').read_text())['Plugins'][0];name=metadata['Name']
            if name in registry.plugins:return []
            plugin=SimpleNamespace(name=name,resourcePath=str(resource),metadata=metadata['Info'])
            registry.plugins[name]=plugin;registry.returned.append(name);return [plugin]
        registry.RegisterPlugins=Mock(side_effect=register);registry.GetPluginWithName=lambda name:registry.plugins.get(name)
        registry.FindTypeByName=lambda name:name if name in types else None
        registry.GetPluginForType=lambda type:registry.plugins.get(types.get(type))
        usd_registry=SimpleNamespace(FindAppliedAPIPrimDefinition=lambda name:None if registry.definitionMissing==name else definitions.get(name))
        pxr=ModuleType('pxr');pxr.Plug=SimpleNamespace(Registry=lambda:registry);pxr.Usd=SimpleNamespace(SchemaRegistry=lambda:usd_registry)
        self.enter(patch.dict(sys.modules,{'pxr':pxr}))
        return scope,registry,resources

    def test_fixed_resources_are_complete_repeatable_and_before_schema_cache_use(self):
        scope,registry,_=self.fixture();scope['_initialize_usd_schemas']();first=list(registry.returned)
        scope['_initialize_usd_schemas']();self.assertEqual(registry.returned,first);self.assertEqual(len(first),6)
        self.assertIn('omniSensor',registry.plugins)

    def test_missing_changed_provider_wrong_sdk_or_preoccupied_name_is_rejected(self):
        for bad in ('missing','changed','sdk','preoccupied','metadata','definition','type_owner'):
            with self.subTest(bad=bad):
                scope,registry,resources=self.fixture();resource=next(path for path,name,_ in resources if name=='RobotSchema')
                if bad=='missing':(resource/'plugInfo.json').unlink()
                elif bad=='changed':(resource/'generatedSchema.usda').write_text('# changed source\n')
                elif bad=='sdk':scope['importlib'].metadata.version=lambda _:'wrong'
                elif bad=='preoccupied':registry.plugins['RobotSchema']=SimpleNamespace(resourcePath='/wrong',metadata={})
                elif bad=='metadata':registry.plugins['RobotSchema']=SimpleNamespace(resourcePath=str(resource),metadata={'wrong':True})
                elif bad=='definition':registry.definitionMissing='IsaacRobotAPI'
                else:
                    registry.GetPluginForType=lambda _:SimpleNamespace(name='wrong')
                with self.assertRaises((ValueError,RuntimeError,FileNotFoundError)):scope['_initialize_usd_schemas']()

    def test_cpu_environment_contains_abi_and_fixed_plugin_roots_before_import(self):
        scope,_,resources=self.fixture();environment=scope['_cpu_environment']()
        self.assertNotIn('pxr',scope)
        self.assertIn('omni.usd.schema.physx-',environment['PYTHONPATH']);self.assertIn('omni.usd.schema.physx-',environment['LD_LIBRARY_PATH'])
        self.assertEqual(environment['PXR_PLUGINPATH_NAME'].split(os.pathsep),[str(path) for path,_,_ in resources])

    def test_real_submit_passes_cpu_environment_to_its_original_parent_popen(self):
        temporary=Path(self.enter(tempfile.TemporaryDirectory()));events=[]
        process=SimpleNamespace(pid=12345,wait=lambda:None)
        class Thread:
            def __init__(self,**kwargs):pass
            def start(self):pass
        context=__import__('contextlib').nullcontext
        scope={'Path':Path,'json':json,'hashlib':hashlib,'subprocess':SimpleNamespace(Popen=Mock(return_value=process),DEVNULL=-3),
            'sys':SimpleNamespace(executable='/exact/sdk/python'),'atomic_json':lambda p,v:p.write_text(json.dumps(v)),
            'valid_id':lambda value:value,'append_event':lambda *a:None,'terminal_lock':lambda *a:context(),
            'inspect':lambda *a:{'state':'running'},'_token':lambda pid:'fixture','time':SimpleNamespace(time=lambda:0)}
        environment={'PYTHONPATH':'fixed-usd-abi','LD_LIBRARY_PATH':'fixed-libraries','PXR_PLUGINPATH_NAME':'fixed-schemas'}
        sim=ModuleType('runtime.simulation');sim._cpu_environment=Mock(return_value=environment)
        runtime=ModuleType('runtime');runtime.simulation=sim
        threading=ModuleType('threading');threading.Thread=Thread
        functions('jobs.py',{'submit'},scope)
        with patch.dict(sys.modules,{'runtime':runtime,'runtime.simulation':sim,'threading':threading}):
            scope['submit'](temporary/'code',temporary/'data',{'jobId':'one','requestId':'one','task':{'operation':'health'}})
        command=scope['subprocess'].Popen.call_args
        self.assertEqual(command.args[0],['/exact/sdk/python',str(temporary/'code/worker.py'),'--execute-job',str(temporary/'data/jobs/one')])
        self.assertEqual(command.kwargs.get('env'),environment,'production CPU parent still starts without the ABI environment')
        self.assertTrue(command.kwargs['start_new_session']);self.assertTrue(command.kwargs['close_fds'])

    def test_isolated_native_builder_initializes_fixed_schemas_before_creating_its_stage(self):
        tree=ast.parse((ROOT/'runtime/worker.py').read_text())
        function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='native_build_entry')
        calls=[node for node in ast.walk(function) if isinstance(node,ast.Call)]
        initialize=[node for node in calls if isinstance(node.func,ast.Name) and node.func.id=='_initialize_usd_schemas']
        stage=[node for node in calls if isinstance(node.func,ast.Attribute) and node.func.attr=='CreateNew']
        self.assertEqual(len(initialize),1)
        self.assertLess(initialize[0].lineno,min(node.lineno for node in stage))


if __name__=='__main__':unittest.main()
