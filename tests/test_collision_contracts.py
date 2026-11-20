import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.scene import World



def tetra_hull():
    return {'vertices': [[0., 0., 0.], [1., 0., 0.], [0., 1., 0.], [0., 0., 1.]],
            'indices': [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3],
            'polygons': [{'indexBase': 0, 'numVertices': 3, 'plane': [0., 0., -1., 0.]},
                         {'indexBase': 3, 'numVertices': 3, 'plane': [0., -1., 0., 0.]},
                         {'indexBase': 6, 'numVertices': 3, 'plane': [-1., 0., 0., 0.]},
                         {'indexBase': 9, 'numVertices': 3, 'plane': [1., 1., 1., -1.]}]}


class CollisionSourceContractsTests(unittest.TestCase):
    def test_asset_modes_and_material_survive_restore_context_and_diff(self):
        world = World()
        world.asset('tray', '/prepared/tray.glb', (1, 1, .2), 'Z', collision_mode='convex_decomposition',
                    mass=.1, physics_material={'static_friction': .8, 'dynamic_friction': .5, 'restitution': .05})
        self.assertNotEqual(world.nodes['tray'].get('collision_fidelity'), 'bounding_box')
        self.assertEqual(World.restore(world.snapshot()).snapshot(), world.snapshot())
        self.assertEqual(world.context('tray')['subtree'][0]['collision_mode'], 'convex_decomposition')
        changed = World.restore(world.snapshot())
        changed.nodes['tray']['collision_mode'] = 'box'
        self.assertIn('collision_mode', changed.diff(world)['changed'][0]['fields'])

    def test_invalid_modes_dynamic_mesh_and_compound_conflicts_are_rejected(self):
        invalid = [{'collision_mode': 'convexHull'}, {'collision_mode': 'mesh', 'mass': .1},
                   {'collision_mode': 'mesh', 'joint': {'type': 'hinge', 'range': [0, 1]}},
                   {'collision_mode': 'mesh', 'collision_boxes': [{'size': [1, 1, 1], 'at': [0, 0, 0], 'orientation': [1, 0, 0, 0]}]},
                   {'collision_boxes': []}, {'collision_boxes': [{'size': [-1, 1, 1], 'at': [0, 0, 0], 'orientation': [1, 0, 0, 0]}]},
                   {'collision_boxes': [{'size': [1, 1, 1], 'at': [float('nan'), 0, 0], 'orientation': [1, 0, 0, 0]}]},
                   {'collision_boxes': [{'size': [1, 1, 1], 'at': [0, 0, 0], 'orientation': [2, 0, 0, 0]}]},
                   {'collision_boxes': [{'size': [1, 1, 1], 'at': [0, 0, 0], 'orientation': [1, 0, 0, 0], 'mesh': 'x'}]}]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, 'collision'):
                World().asset('tray', '/prepared/tray.glb', (1, 1, 1), 'Z', **fields)

    def test_material_validates_at_source_and_restored_snapshot(self):
        for material in ({'static_friction': -.1}, {'dynamic_friction': float('inf')},
                         {'restitution': -1}, {'restitution': 1.1}, {'friction': .4}, []):
            with self.subTest(material=material), self.assertRaisesRegex(ValueError, 'physics_material'):
                World().box('cube', (1, 1, 1), physics_material=material)
        world = World()
        world.box('cube', (1, 1, 1))
        snapshot = world.snapshot()
        snapshot['nodes'][0]['physics_material'] = {'dynamic_friction': -.1}
        with self.assertRaisesRegex(ValueError, 'physics_material'):
            World.restore(snapshot)

    def test_legacy_asset_and_explicit_disabled_collision_keep_old_meaning(self):
        world = World()
        world.asset('tray', '/prepared/tray.glb', (1, 1, .2), 'Z')
        snapshot = world.snapshot()
        snapshot['nodes'][0].pop('collision_mode', None)
        snapshot['nodes'][0]['collision_fidelity'] = 'bounding_box'
        restored = World.restore(snapshot)
        self.assertEqual(restored.nodes['tray'].get('collision_mode', 'box'), 'box')
        world.asset('marker', '/prepared/marker.glb', (.1, .1, .1), 'Z', collision=False)
        self.assertIs(world.nodes['marker']['collision'], False)


class CollisionCompilationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        import trimesh
        from pxr import Gf, Usd, UsdGeom, UsdPhysics, UsdShade
        from runtime import simulation
        cls.np, cls.trimesh, cls.Gf, cls.Usd = np, trimesh, Gf, Usd
        cls.UsdGeom, cls.UsdPhysics, cls.UsdShade, cls.simulation = UsdGeom, UsdPhysics, UsdShade, simulation

    def fixture(self, directory, mode='mesh', up_axis='Z', **fields):
        mesh = self.trimesh.creation.box(extents=[1., .6, .4])
        mesh.apply_translation([.6, -.2, .9])
        if up_axis == 'Y':
            mesh.apply_transform(self.trimesh.transformations.rotation_matrix(-self.np.pi/2, [1, 0, 0]))
        path = directory/'visual.glb'
        path.write_bytes(self.trimesh.Scene(mesh).export(file_type='glb'))
        world = World()
        world.space('room', at=(3, 2, 1), yaw=.7)
        world.asset('tray', path, (.5, .4, .3), up_axis, frame='room', collision_mode=mode, **fields)
        self.simulation.normalize_assets(world)
        return world, world.nodes['tray']

    def stage(self):
        stage = self.Usd.Stage.CreateInMemory()
        self.UsdGeom.SetStageMetersPerUnit(stage, 1)
        self.UsdGeom.SetStageUpAxis(stage, self.UsdGeom.Tokens.z)
        self.UsdGeom.Xform.Define(stage, '/World')
        self.UsdGeom.Xform.Define(stage, '/World/tray')
        return stage

    def collision_fixture(self, directory, mode='convex_decomposition', enabled=True, boxes=None):
        fields = {'collision': enabled}
        if boxes is not None:
            fields['collision_boxes'] = boxes
        world, node = self.fixture(directory, mode=mode, **fields)
        identity = {'fixture': 'recorded-native-CPU-only'}
        with patch.object(self.simulation, 'collision_runtime_identity', return_value=identity):
            locked = self.simulation.lock_collision_inputs(world, directory)
            def recorded_cook(geometry, output, inputs):
                output.mkdir(parents=True, exist_ok=True)
                raw = json.loads((Path(__file__).parent/'fixtures/native-convex-asymmetric-u.json').read_text())
                (output/'cook-result.json').write_text(json.dumps({'result': 'RESULT_VALID', 'hulls': raw['hulls'],
                    'geometrySha256': self.simulation._collision_geometry_digest(geometry),
                    'recipe': inputs['recipe'], 'nativeRuntime': inputs['nativeRuntime']}))
                return raw['hulls']
            with patch.object(self.simulation, '_cook_convex_collision', side_effect=recorded_cook):
                stage = self.stage()
                row = self.simulation._write_node_collision(stage, '/World/tray', node,
                    self.simulation._asset_parts(node), directory, locked)
                stage.GetRootLayer().Export(str(directory/'scene.usda'))
        return self.Usd.Stage.Open(str(directory/'scene.usda'))

    def test_locked_mesh_is_centered_zup_and_scale_baked_once_without_world_pose(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            world, node = self.fixture(directory, up_axis='Y')
            before = world.snapshot()
            locked = self.simulation.lock_collision_inputs(world, directory)
            row = locked['nodes']['tray']
            geometry = self.simulation._collision_geometry(self.simulation._asset_parts(node))
            points = self.np.asarray(geometry['points'])
            self.np.testing.assert_allclose(points.mean(axis=0), 0, atol=1e-7, rtol=0)
            self.np.testing.assert_allclose(self.np.ptp(points, axis=0), node['actualSize'], atol=1e-7, rtol=0)
            self.assertEqual(row['geometrySha256'], self.simulation._collision_geometry_digest(geometry))
            self.assertEqual(row['normalization']['sourceUpAxis'], 'Y')
            self.assertEqual(row['normalization']['uniformScale'], node['scale'])
            self.assertEqual(world.snapshot(), before, 'locking must not mutate World')
            self.assertNotIn('worldFromMesh', row)
            self.assertEqual(json.loads((directory/'dependencies/collision-inputs.json').read_text()), locked)

    def test_recipe_identity_and_canonical_geometry_change_locked_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            world, node = self.fixture(directory, mode='convex_decomposition', mass=.1)
            with patch.object(self.simulation, 'collision_runtime_identity', return_value={'isaacsim': '6.0.0.1', 'binaries': {'cooker': 'a'}}):
                original = self.simulation.lock_collision_inputs(world, directory)
            with patch.object(self.simulation, 'collision_runtime_identity', return_value={'isaacsim': '6.0.0.1', 'binaries': {'cooker': 'b'}}):
                changed_runtime = self.simulation.lock_collision_inputs(world, directory)
            self.assertNotEqual(original, changed_runtime)
            node['requestedSize'] = [.25, .2, .15]
            self.simulation.normalize_assets(world)
            with patch.object(self.simulation, 'collision_runtime_identity', return_value={'isaacsim': '6.0.0.1', 'binaries': {'cooker': 'a'}}):
                changed_geometry = self.simulation.lock_collision_inputs(world, directory)
            self.assertNotEqual(original['nodes']['tray']['geometrySha256'], changed_geometry['nodes']['tray']['geometrySha256'])

    def test_static_mesh_uses_actual_normalized_visual_triangles_and_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            world, node = self.fixture(directory, physics_material={'static_friction': .8, 'dynamic_friction': .5, 'restitution': .05})
            locked = self.simulation.lock_collision_inputs(world, directory)
            parts = self.simulation._asset_parts(node)
            geometry = self.simulation._collision_geometry(parts)
            stage = self.stage()
            result = self.simulation._write_node_collision(stage, '/World/tray', node, parts, directory, locked)
            mesh = self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/tray/collision'))
            self.np.testing.assert_array_equal(mesh.GetPointsAttr().Get(), geometry['points'])
            self.assertEqual(list(mesh.GetFaceVertexIndicesAttr().Get()), geometry['indices'])
            self.assertEqual(self.UsdPhysics.MeshCollisionAPI(mesh).GetApproximationAttr().Get(), 'none')
            self.assertTrue(mesh.GetPrim().HasAPI(self.UsdPhysics.CollisionAPI))
            material, _ = self.UsdShade.MaterialBindingAPI(mesh).ComputeBoundMaterial(materialPurpose='physics')
            api = self.UsdPhysics.MaterialAPI(material.GetPrim())
            self.assertAlmostEqual(api.GetStaticFrictionAttr().Get(), .8)
            self.assertAlmostEqual(api.GetDynamicFrictionAttr().Get(), .5)
            self.assertAlmostEqual(api.GetRestitutionAttr().Get(), .05)
            self.assertFalse(result['physicalContactVerified'])

    def test_native_return_is_frozen_and_read_as_convex_children_without_second_scale(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            world, node = self.fixture(directory, mode='convex_decomposition', mass=.1)
            identity = {'isaacsim': '6.0.0.1', 'binaries': {'cooker': 'fixture'}}
            with patch.object(self.simulation, 'collision_runtime_identity', return_value=identity):
                locked = self.simulation.lock_collision_inputs(world, directory)
                stage = self.stage()
                with patch.object(self.simulation, '_cook_convex_collision', return_value=[tetra_hull()]) as cook:
                    result = self.simulation._write_node_collision(stage, '/World/tray', node,
                        self.simulation._asset_parts(node), directory, locked)
            cook.assert_called_once()
            mesh = self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/tray/collision/h_0'))
            self.np.testing.assert_array_equal(mesh.GetPointsAttr().Get(), tetra_hull()['vertices'])
            self.assertEqual(mesh.GetOrderedXformOps(), [])
            self.assertEqual(self.UsdPhysics.MeshCollisionAPI(mesh).GetApproximationAttr().Get(), 'convexHull')
            frozen = self.Usd.Stage.Open(str(directory/result['artifact']))
            self.np.testing.assert_array_equal(self.UsdGeom.Mesh(frozen.GetPrimAtPath('/Collision/h_0')).GetPointsAttr().Get(), tetra_hull()['vertices'])
            self.assertFalse(result['physicalContactVerified'])
            self.assertEqual(result['hullCount'], 1)

    def test_changed_lock_is_rejected_before_cooking_or_authoring(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            world, node = self.fixture(directory)
            locked = self.simulation.lock_collision_inputs(world, directory)
            node['scale'] *= .9
            with patch.object(self.simulation, '_cook_convex_collision', side_effect=AssertionError('cooked changed input')):
                with self.assertRaisesRegex(ValueError, 'locked collision'):
                    self.simulation._write_node_collision(self.stage(), '/World/tray', node,
                        self.simulation._asset_parts(node), directory, locked)

    def test_compound_boxes_and_disabled_collision_keep_geometry_and_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            boxes = [{'size': [.3, .2, .04], 'at': [0, 0, -.08], 'orientation': [1, 0, 0, 0]},
                     {'size': [.04, .2, .15], 'at': [-.13, 0, 0], 'orientation': [1, 0, 0, 0]}]
            world, node = self.fixture(directory, mode='box', collision_boxes=boxes)
            locked = self.simulation.lock_collision_inputs(world, directory)
            stage = self.stage()
            result = self.simulation._write_node_collision(stage, '/World/tray', node, [], directory, locked)
            self.assertEqual(result['shapeCount'], 2)
            for index, box in enumerate(boxes):
                shape = self.UsdGeom.Cube(stage.GetPrimAtPath('/World/tray/collision/box_'+str(index)))
                self.assertTrue(shape.GetPrim().HasAPI(self.UsdPhysics.CollisionAPI))
                self.np.testing.assert_allclose(self.simulation._usd_world(stage, str(shape.GetPath()))[:3, 3], box['at'])
            node['collision'] = False
            disabled = self.simulation.lock_collision_inputs(world, directory)
            other = self.stage()
            result = self.simulation._write_node_collision(other, '/World/tray', node, [], directory, disabled)
            self.assertFalse(result['enabled'])
            self.assertFalse(any(prim.HasAPI(self.UsdPhysics.CollisionAPI) for prim in other.Traverse()))

    def test_empty_invalid_or_nonclosed_convex_results_are_rejected(self):
        invalid = [[], [{**tetra_hull(), 'vertices': [[float('nan'), 0, 0]]}],
                   [{**tetra_hull(), 'indices': [999]*12}],
                   [{**tetra_hull(), 'polygons': tetra_hull()['polygons'][:-1]}],
                   [{**tetra_hull(), 'vertices': [[0, 0, 0]]*4}]]
        for hulls in invalid:
            with self.subTest(hulls=hulls), self.assertRaisesRegex(ValueError, 'convex'):
                self.simulation._validate_convex_hulls(hulls)

    def test_previously_recorded_real_native_hulls_pass_geometry_validation(self):
        import hashlib
        path = Path(__file__).parent/'fixtures/native-convex-asymmetric-u.json'
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                         '4cb7a1423a03e163422c303eea8daacdea3ade2ac8c8f11d23566a5ae5e98db4')
        result = json.loads(path.read_text())
        self.assertEqual(result['result'], 'PhysxCollisionRepresentationResult.RESULT_VALID')
        self.assertEqual(len(result['hulls']), 4)
        self.simulation._validate_convex_hulls(result['hulls'])

    def test_native_start_failure_is_preserved_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            world, node = self.fixture(directory, mode='convex_decomposition')
            with patch.object(self.simulation, 'collision_runtime_identity', return_value={'fixture': 'CPU'}):
                locked = self.simulation.lock_collision_inputs(world, directory)
            geometry = self.simulation._collision_geometry(self.simulation._asset_parts(node))
            with patch.object(self.simulation, '_ensure_app', side_effect=RuntimeError('fixture SDK start failure')):
                with self.assertRaisesRegex(RuntimeError, 'SDK start failure'):
                    self.simulation._cook_convex_collision(geometry, directory/'failed', locked)
            report = json.loads((directory/'failed/cook-failure.json').read_text())
            self.assertFalse(report['fallbackUsed'])
            self.assertIn('fixture SDK start failure', report['error'])
            self.assertFalse((directory/'failed/collision.usda').exists())

    def test_native_timeout_cancels_and_saves_failure_without_using_a_box(self):
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest.mock import Mock
        import pxr
        class Recipe:
            def __init__(self):self.values={}
            def __getattr__(self,name):
                if name.startswith('Create'):
                    return lambda value:self.values.__setitem__(name[6:],value)
                if name.startswith('Get'):
                    return lambda:SimpleNamespace(Get=lambda:self.values[name[3:]])
                raise AttributeError(name)
        schema=SimpleNamespace(PhysxConvexDecompositionCollisionAPI=SimpleNamespace(Apply=lambda prim:Recipe()))
        paths=SimpleNamespace(sdfPathToInt=lambda path:1)
        cooking = Mock()
        native, physx = ModuleType('omni'), ModuleType('omni.physx')
        native.physx = physx
        physx.get_physx_cooking_interface = lambda: cooking
        bindings = ModuleType('omni.physx.bindings._physx')
        bindings.PhysxCollisionRepresentationResult = SimpleNamespace(RESULT_VALID=0)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            world, node = self.fixture(directory, mode='convex_decomposition')
            with patch.object(self.simulation, 'collision_runtime_identity', return_value={'fixture': 'CPU'}):
                locked = self.simulation.lock_collision_inputs(world, directory)
            geometry = self.simulation._collision_geometry(self.simulation._asset_parts(node))
            with patch.object(pxr,'PhysxSchema',schema,create=True), patch.object(pxr,'PhysicsSchemaTools',paths,create=True), \
                    patch.dict(sys.modules, {'omni': native, 'omni.physx': physx, 'omni.physx.bindings._physx': bindings}), \
                    patch.object(self.simulation, '_ensure_app', return_value=SimpleNamespace(update=Mock())), \
                    patch.object(self.simulation.time, 'monotonic', side_effect=[0, 100]):
                with self.assertRaises(TimeoutError):
                    self.simulation._cook_convex_collision(geometry, directory/'timeout', locked)
            cooking.cancel_collision_representation_task.assert_called_once()
            failure = json.loads((directory/'timeout/cook-failure.json').read_text())
            self.assertFalse(failure['fallbackUsed'])
            self.assertTrue((directory/'timeout/cook-source.usda').is_file())













    def test_actual_composed_hull_geometry_and_collision_flags_cannot_be_overridden(self):
        hulls=json.loads((Path(__file__).parent/'fixtures/native-convex-asymmetric-u.json').read_text())['hulls']
        for change in ('points','approximation','disabled','hull_scale','collision_scale','hull_reset','collision_reset'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);stage=self.collision_fixture(directory)
                path='/World/tray/collision'
                self.simulation.verify_convex_collision(stage,path,hulls)
                prim=stage.GetPrimAtPath(path+'/h_0')
                if change=='points':
                    mesh=self.UsdGeom.Mesh(prim);values=self.np.asarray(mesh.GetPointsAttr().Get())+[1,0,0]
                    mesh.GetPointsAttr().Set([self.Gf.Vec3f(*p) for p in values])
                elif change=='approximation':self.UsdPhysics.MeshCollisionAPI(prim).CreateApproximationAttr('boundingCube')
                elif change=='disabled':self.UsdPhysics.CollisionAPI(prim).CreateCollisionEnabledAttr(False)
                else:
                    if change.startswith('collision'):prim=stage.GetPrimAtPath(path)
                    xform=self.UsdGeom.Xformable(prim)
                    if change.endswith('scale'):xform.AddScaleOp().Set(self.Gf.Vec3f(10))
                    else:xform.SetResetXformStack(True)
                with self.assertRaisesRegex(ValueError,'collision|hull|convex'):
                    self.simulation.verify_convex_collision(stage,path,hulls)



if __name__ == '__main__':
    unittest.main()
