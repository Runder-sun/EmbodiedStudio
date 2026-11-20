"""Isolated system fixtures: USD authoring/reload; no renderer or physics claim."""
import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from runtime.scene import World, Task, revision_key


class LightingSourceTests(unittest.TestCase):
    def invalid_typed_lights(self):
        sphere = {'id': 'lamp', 'kind': 'sphere', 'intensity': 100, 'at': [0, 0, 1], 'frame': None,
                  'orientation': [1, 0, 0, 0], 'color': [1, 1, 1], 'radius': .1}
        for field, value in [('radius', '0.1'), ('intensity', '100'), ('color', '111'),
                             ('orientation', '1000'), ('at', '123'), ('at', [1, '2', 3]),
                             ('color', [True, 1, 1]), ('orientation', [1, False, 0, 0]),
                             ('radius', True), ('intensity', True), ('at', {'1': 1, '2': 2, '3': 3})]:
            yield {**sphere, field: value}
        rect = {**sphere, 'kind': 'rect', 'width': 1, 'height': .5}
        del rect['radius']
        yield {**rect, 'width': '1'}
        yield {**rect, 'height': '.5'}

    def test_constructor_rejects_string_or_boolean_light_values(self):
        for light in self.invalid_typed_lights():
            with self.subTest(light=light), self.assertRaises(ValueError):
                World(lights=[light])

    def test_restore_rejects_string_or_boolean_light_values(self):
        for light in self.invalid_typed_lights():
            snapshot = World().snapshot()
            snapshot['lights'] = [light]
            with self.subTest(light=light), self.assertRaises(ValueError):
                World.restore(snapshot)

    def test_light_entry_rejects_string_or_boolean_light_values(self):
        for light in self.invalid_typed_lights():
            with self.subTest(light=light), self.assertRaises(ValueError):
                World().light(**light)

    def test_missing_lights_preserves_legacy_snapshot_and_empty_is_explicit(self):
        world = World()
        old = world.snapshot()
        self.assertNotIn('lights', old)
        self.assertIsNone(World.restore(old).lights)
        dark = World(lights=[])
        self.assertEqual(dark.snapshot()['lights'], [])
        self.assertEqual(World.restore(dark.snapshot()).lights, [])
        self.assertNotEqual(revision_key(world=old), revision_key(world=dark.snapshot()))

    def test_lights_restore_context_diff_revision_and_input_copy(self):
        world = World()
        world.space('room', at=(3, 2, 1), yaw=math.pi/2)
        world.light('ceiling', 'rect', 2000, frame='room', at=(.5, 0, 2.5), width=1.2, height=.4)
        world.light('desk', 'sphere', 100, at=(2, 2, 1), radius=.05, color=(1, .8, .6))
        snapshot = world.snapshot()
        self.assertEqual(World.restore(snapshot).snapshot(), snapshot)
        self.assertEqual(world.context()['lights'], snapshot['lights'])
        self.assertEqual(world.context('room')['lights'], snapshot['lights'])
        changed = World.restore(snapshot)
        changed.lights[0]['intensity'] = 2500
        difference = world.diff(changed)
        self.assertEqual(difference['world']['lights']['before'], snapshot['lights'])
        self.assertEqual(difference['world']['lights']['after'][0]['intensity'], 2500)
        self.assertNotEqual(difference['baseHash'], difference['sourceHash'])
        snapshot['lights'][0]['at'][0] = 100
        self.assertEqual(world.lights[0]['at'][0], .5)

    def test_invalid_declarations_reject_nonfinite_shape_frame_color_and_orientation(self):
        fields = [{'kind': 'dome'}, {'intensity': 0}, {'intensity': -1}, {'intensity': float('inf')},
                  {'intensity': float('nan')}, {'at': [float('nan'), 0, 0]}, {'at': [0, 0]},
                  {'orientation': [0, 0, 0, 0]}, {'orientation': [2, 0, 0, 0]},
                  {'orientation': [float('inf'), 0, 0, 0]}, {'color': [1, .5]},
                  {'color': [1.1, .5, .5]}, {'color': [-.1, .5, .5]},
                  {'radius': 0}, {'radius': float('inf')}, {'width': 1}, {'frame': 'missing'}]
        for change in fields:
            arguments = {'kind': 'sphere', 'intensity': 100, 'radius': .1, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                World().light('lamp', **arguments)
        for dimensions in ({'width': 0, 'height': 1}, {'width': 1, 'height': float('nan')},
                           {'width': 1}, {'width': 1, 'height': 1, 'radius': .1}):
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                World().light('lamp', 'rect', 100, **dimensions)
        world = World()
        world.light('lamp', 'sphere', 100, radius=.1)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            world.light('lamp', 'sphere', 100, radius=.1)
        with self.assertRaises(ValueError):
            world.light('bad/id', 'sphere', 100, radius=.1)

    def test_restore_rejects_invalid_and_duplicate_lights_instead_of_defaulting(self):
        world = World()
        world.light('lamp', 'sphere', 100, radius=.1)
        for change in ({'intensity': float('nan')}, {'orientation': [2, 0, 0, 0]},
                       {'radius': -.1}, {'frame': 'missing'}, {'kind': 'dome'}, {'exposure': 2}):
            bad = world.snapshot()
            bad['lights'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                World.restore(bad)
        bad = world.snapshot()
        bad['lights'].append(copy.deepcopy(bad['lights'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            World.restore(bad)
        for value in ({}, 'default'):
            bad = world.snapshot()
            bad['lights'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                World.restore(bad)

    def test_scoped_context_keeps_other_light_frame_dependencies(self):
        world = World()
        world.space('room_a')
        world.space('wing_b', at=(4, 0, 0))
        world.space('room_b', frame='wing_b')
        world.light('window_light', 'rect', 100, frame='room_b', width=1, height=1)
        context = world.context('room_a')
        self.assertEqual([light['id'] for light in context['lights']], ['window_light'])
        self.assertEqual({node['id'] for node in context['externalNodes']}, {'wing_b', 'room_b'})


class LightingUsdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        from pxr import Gf, Plug, Usd, UsdGeom, UsdLux
        from runtime import simulation
        for plugin in Path(sys.prefix).glob('lib/python*/site-packages/isaacsim/extscache/omni.usd.schema.omni_sensors-*/usd_plugins'):
            Plug.Registry().RegisterPlugins(str(plugin))
        if Usd.SchemaRegistry().FindAppliedAPIPrimDefinition('OmniSensorAPI') is None:
            raise unittest.SkipTest('Official sensor USD schema unavailable; no SDK startup in CPU tests')
        cls.np, cls.Gf, cls.Usd, cls.UsdGeom, cls.UsdLux, cls.simulation = np, Gf, Usd, UsdGeom, UsdLux, simulation

    def fixture(self, directory, lights=None):
        # Native source-only USD fixture, never a generated robot or product.
        stage=self.Usd.Stage.CreateNew(str(directory/'scene-source.usda'))
        stage.SetDefaultPrim(self.UsdGeom.Xform.Define(stage,'/World').GetPrim())
        self.UsdGeom.SetStageMetersPerUnit(stage,1)
        self.UsdGeom.SetStageUpAxis(stage,self.UsdGeom.Tokens.z)
        world=World(native=True,lights=lights);world.stage=stage
        world.space('building',at=(2,3,1),yaw=math.pi/2)
        world.space('room',at=(1,0,2),frame='building',orientation=(math.sqrt(.5),math.sqrt(.5),0,0))
        world.box('cube',(.05,.05,.05))
        task=Task('lighting',bindings={'subject':'cube'},rubric={'sourceRef':'rubrics.py','helperRefs':[]})
        task.bind(world)
        return world,task

    def author(self,world,directory):
        result=self.simulation.author_native_helpers(world,world.stage,directory)
        world.stage.GetRootLayer().Save()
        return result


    def light_paths(self, stage):
        return [str(prim.GetPath()) for prim in stage.Traverse() if prim.HasAPI(self.UsdLux.LightAPI)]

    def test_default_keeps_fill_key_without_task_specialized_lights(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);world,task=self.fixture(directory)
            self.author(world,directory)
            stage=self.Usd.Stage.Open(str(directory/'scene-source.usda'))
            self.assertEqual(set(self.light_paths(stage)),{'/World/lights/fill','/World/lights/key'})
            self.assertEqual(self.UsdLux.DomeLight(stage.GetPrimAtPath('/World/lights/fill')).GetIntensityAttr().Get(),450)
            self.assertEqual(self.UsdLux.SphereLight(stage.GetPrimAtPath('/World/lights/key')).GetIntensityAttr().Get(),30000)


    def test_explicit_empty_writes_no_automatic_lights(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);world,task=self.fixture(directory,lights=[])
            self.author(world,directory)
            stage=self.Usd.Stage.Open(str(directory/'scene-source.usda'))
            self.assertEqual(self.light_paths(stage),[])
            self.assertEqual(world.lights,[])


    def test_real_numeric_vectors_from_all_three_entries_write_valid_usd(self):
        declaration = {'id': 'lamp', 'kind': 'sphere', 'intensity': 100, 'at': [1, 2, 3], 'frame': None,
                       'orientation': (1, 0, 0, 0), 'color': [1, .5, 0], 'radius': .1}
        for entry in ('constructor', 'restore', 'light'):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                world, task = self.fixture(directory, lights=[declaration] if entry == 'constructor' else None)
                if entry == 'restore':
                    native_stage=world.stage
                    snapshot = world.snapshot()
                    snapshot['lights'] = [declaration]
                    world = World.restore(snapshot)
                    world.stage=native_stage
                elif entry == 'light':
                    world.light(**declaration)
                task.bind(world)
                result = self.author(world,directory)
                stage = self.Usd.Stage.Open(str(directory/'scene-source.usda'))
                path = '/World/lights/l_'+declaration['id'].encode().hex()
                lamp = self.UsdLux.SphereLight(stage.GetPrimAtPath(path))
                self.assertEqual(lamp.GetIntensityAttr().Get(), 100)
                self.assertAlmostEqual(lamp.GetRadiusAttr().Get(), .1)
                self.np.testing.assert_array_equal(lamp.GetColorAttr().Get(), [1, .5, 0])
                self.np.testing.assert_array_equal(self.simulation._usd_world(stage, path)[:3, 3], [1, 2, 3])
                self.assertEqual(world.lights[0]['id'],'lamp')

    def test_declarations_write_exact_shapes_world_poses_and_reload_in_new_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            world, task = self.fixture(directory)
            world.light('ceiling', 'rect', 1234, at=(.5, .4, 2), frame='room',
                        orientation=(math.sqrt(.5), 0, math.sqrt(.5), 0), color=(.4, .6, .8), width=1.2, height=.4)
            world.light('desk', 'sphere', 100, at=(1, 2, 3), radius=.05)
            result = self.author(world,directory)
            stage = self.Usd.Stage.Open(str(directory/'scene-source.usda'))
            paths = ['/World/lights/l_'+id.encode().hex() for id in ('ceiling', 'desk')]
            self.assertEqual(set(self.light_paths(stage)), set(paths))
            rect = self.UsdLux.RectLight(stage.GetPrimAtPath(paths[0]))
            sphere = self.UsdLux.SphereLight(stage.GetPrimAtPath(paths[1]))
            self.assertTrue(rect)
            self.assertTrue(sphere)
            self.assertAlmostEqual(rect.GetWidthAttr().Get(), 1.2)
            self.assertAlmostEqual(rect.GetHeightAttr().Get(), .4)
            self.assertAlmostEqual(sphere.GetRadiusAttr().Get(), .05)
            self.assertEqual(rect.GetIntensityAttr().Get(), 1234)
            self.np.testing.assert_allclose(rect.GetColorAttr().Get(), [.4, .6, .8], atol=1e-7)
            frame_pose = self.simulation._pose(*world.transform('room'))
            local = self.simulation._pose(world.lights[0]['at'], world.lights[0]['orientation'])
            self.np.testing.assert_allclose(self.simulation._usd_world(stage, paths[0]), frame_pose@local, atol=1e-12)
            (directory/'compiled.json').write_text(json.dumps({'helpers':result,'declaredLights':world.lights}))
            code = """import json,sys
from pxr import Usd,UsdLux,UsdGeom
stage=Usd.Stage.Open(sys.argv[1]); compiled=json.load(open(sys.argv[2]))
rows=[]
for prim in stage.Traverse():
    if not prim.HasAPI(UsdLux.LightAPI): continue
    light=UsdLux.LightAPI(prim)
    rows.append({'path':str(prim.GetPath()),'type':prim.GetTypeName(),
                 'intensity':light.GetIntensityAttr().Get(),'color':list(light.GetColorAttr().Get()),
                 'matrix':[list(row) for row in UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0)]})
print(json.dumps({'lights':rows,'declaredLights':compiled['declaredLights']}))
"""
            process = subprocess.run([sys.executable, '-B', '-c', code, str(directory/'scene-source.usda'),
                                      str(directory/'compiled.json')], capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            reloaded = json.loads(process.stdout)
            self.assertEqual(set(row['path'] for row in reloaded['lights']), set(paths))
            self.assertEqual(reloaded['declaredLights'],world.lights)
            self.np.testing.assert_allclose(self.np.asarray(reloaded['lights'][0]['matrix']).T,
                                           self.simulation._usd_world(stage, paths[0]), atol=1e-12)
            self.assertEqual(reloaded['lights'][0]['intensity'], 1234)


if __name__ == '__main__':
    unittest.main()
