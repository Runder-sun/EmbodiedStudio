"""Isolated CPU system fixtures; no generated product or simulation application."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from pxr import Gf, Usd, UsdGeom, UsdPhysics
try:
    from pxr import PhysxSchema
except ImportError:
    PhysxSchema = None  # The Mac has core USD; these existing SDK checks run on the locked 4090 prefix.

from runtime import simulation as s, worker
from runtime.scene import World, revision_key


@unittest.skipIf(PhysxSchema is None, 'requires the locked native SDK PhysxSchema; exercised on the 4090')
class StaticContactTests(unittest.TestCase):
    def shape(self, contact=None, rest=None, **changes):
        source = {name: {'state': 'missing'} if value is None else
                  {'state': 'finite', 'value': float(np.float32(value)), 'authored': True}
                  for name, value in [('contact', contact), ('rest', rest)]}
        return {'path': '/World/table/collision', 'type': 'Cube', 'approximation': None,
                'geometrySha256': 'a'*64, 'ownerClass': 'static', 'source': source, **changes}

    def stage(self):
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageMetersPerUnit(stage, 1)
        UsdGeom.Xform.Define(stage, '/World')
        UsdGeom.Xform.Define(stage, '/World/table')
        cube = UsdGeom.Cube.Define(stage, '/World/table/collision')
        cube.CreateSizeAttr(1)
        UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
        return stage, cube.GetPrim()

    def locked_stage(self):
        stage, prim = self.stage()
        rows = s._static_contact_shapes(stage)
        return stage, prim, s._lock_static_contact_shapes(rows, 1)

    def test_only_unknown_channels_are_set_and_finite_signed_values_survive(self):
        for c, r in [(None, None), (None, -.002), (.004, None), (.02, 0), (0, -.001)]:
            with self.subTest(c=c, r=r):
                row = s._lock_static_contact_shapes([self.shape(c, r)], 1)['shapes'][0]
                for name, original, default in [('contact', c, .001), ('rest', r, 0)]:
                    channel = row[name]
                    self.assertEqual(channel['operation'], 'set' if original is None else 'preserve')
                    self.assertEqual(channel['valueF32'], float(np.float32(default if original is None else original)))
                    self.assertEqual(len(channel['bits']), 8)
                self.assertGreater(row['contact']['valueF32'], row['rest']['valueF32'])

    def test_auto_sentinel_is_unknown_not_zero_and_source_is_locked(self):
        shape = self.shape(None, 0)
        shape['source']['contact'] = {'state': 'auto', 'sentinel': '-inf', 'authored': True}
        row = s._lock_static_contact_shapes([shape], 1)['shapes'][0]
        self.assertEqual(row['source'], shape['source'])
        self.assertEqual(row['contact']['bits'], '3a83126f')
        self.assertEqual(row['rest']['operation'], 'preserve')

    def test_invalid_numeric_relations_sources_units_and_owners_are_rejected(self):
        shapes = [self.shape(c, r) for c, r in [(-.001, 0), (.001, .001), (0, None), (None, .002)]]
        for bad in (float('nan'), float('inf'), -float('inf')):
            row = self.shape(); row['source']['contact'] = {'state': 'finite', 'value': bad, 'authored': True}
            shapes.append(row)
        shapes += [self.shape(ownerClass=owner) for owner in ('dynamic', 'articulation', 'fixed_base')]
        shapes += [self.shape(type='Sphere'), self.shape(type='Mesh', approximation='convexHull')]
        for shape in shapes:
            with self.subTest(shape=shape), self.assertRaises(ValueError):
                s._lock_static_contact_shapes([shape], 1)
        for mpu in (.01, 100, True, float('nan')):
            with self.subTest(mpu=mpu), self.assertRaises(ValueError):
                s._lock_static_contact_shapes([self.shape()], mpu)

    def test_legacy_lock_and_legacy_stage_validation_never_author_recipe(self):
        world = World(); world.box('table', (1, 1, .1))
        with tempfile.TemporaryDirectory() as tmp:
            locked = s.lock_collision_inputs(world, tmp)
            self.assertNotIn('staticContactRecipe', locked)
        stage, prim = self.stage(); before = stage.GetRootLayer().ExportToString()
        s._consume_static_contacts(stage, None)
        self.assertEqual(stage.GetRootLayer().ExportToString(), before)
        self.assertFalse(prim.HasAPI(PhysxSchema.PhysxCollisionAPI))

    def test_opt_in_locks_ground_all_eligible_paths_and_revision_inputs(self):
        world = World(); world.box('table', (1, 1, .1))
        world.cylinder('leg', .02, .4)
        world.box('body', (.1, .1, .1), mass=.1)
        world.box('jointed', (.1, .1, .1), joint={'type': 'slide', 'range': [0, .1]})
        world.box('off', (.1, .1, .1), collision=False)
        world.box('compound', (1, 1, 1), collision_boxes=[{'size': [.2]*3, 'at': [0]*3, 'orientation': [1, 0, 0, 0]},
            {'size': [.1]*3, 'at': [.3, 0, 0], 'orientation': [1, 0, 0, 0]}])
        before = world.snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            old = s.lock_collision_inputs(world, tmp)
            locked = s.lock_collision_inputs(world, tmp, static_contact_recipe='static-unresolved-contact-v1')
            rows = locked['staticContactRecipe']['shapes']
            expected = {'/World/ground/visual'} | {'/World/objects/o_'+i.encode().hex()+'/collision' for i in ('table', 'leg')}
            expected |= {'/World/objects/o_'+b'compound'.hex()+'/collision/box_'+str(i) for i in range(2)}
            self.assertEqual({row['path'] for row in rows}, expected)
            self.assertEqual(world.snapshot(), before)
            self.assertEqual(json.loads((Path(tmp)/'dependencies/collision-inputs.json').read_text()), locked)
            self.assertNotEqual(revision_key(collisionInputs=old), revision_key(collisionInputs=locked))
            for key in ('geometrySha256', 'source', 'contact', 'path'):
                changed = deepcopy(locked); changed['staticContactRecipe']['shapes'][0][key] = 'changed'
                self.assertNotEqual(revision_key(collisionInputs=locked), revision_key(collisionInputs=changed))

    def test_static_convex_paths_are_inapplicable_without_a_new_cooking_pipeline(self):
        world = World(); world.box('static', (1, 1, 1), collision_mode='convex_decomposition')
        with tempfile.TemporaryDirectory() as tmp, patch.object(s, 'collision_runtime_identity', return_value={'test': 'CPU'}):
            with self.assertRaisesRegex(ValueError, 'static.*convex|prelock'):
                s.lock_collision_inputs(world, tmp, static_contact_recipe='static-unresolved-contact-v1')

    def test_author_consumes_all_locked_rows_then_reopen_only_verifies(self):
        stage, prim, locked = self.locked_stage()
        s._consume_static_contacts(stage, locked, author=True)
        self.assertAlmostEqual(prim.GetAttribute('physxCollision:contactOffset').Get(), float(np.float32(.001)))
        self.assertEqual(prim.GetAttribute('physxCollision:restOffset').Get(), 0)
        self.assertFalse(prim.HasAPI(UsdPhysics.RigidBodyAPI))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'static.usda'; stage.GetRootLayer().Export(str(path))
            reopened = Usd.Stage.Open(str(path)); before = reopened.GetRootLayer().ExportToString()
            s._consume_static_contacts(reopened, locked)
            self.assertEqual(reopened.GetRootLayer().ExportToString(), before)
            reopened.GetPrimAtPath(str(prim.GetPath())).GetAttribute('physxCollision:contactOffset').Set(.002)
            changed = reopened.GetRootLayer().ExportToString()
            with self.assertRaises(ValueError): s._consume_static_contacts(reopened, locked)
            self.assertEqual(reopened.GetRootLayer().ExportToString(), changed)

    def test_existing_finite_attributes_and_dynamic_robot_actors_are_unchanged(self):
        stage, prim = self.stage()
        api = PhysxSchema.PhysxCollisionAPI.Apply(prim)
        api.CreateContactOffsetAttr(0); api.CreateRestOffsetAttr(-.001)
        for path in ('/World/body', '/World/franka/panda_leftfinger'):
            actor = UsdGeom.Xform.Define(stage, path).GetPrim(); UsdPhysics.RigidBodyAPI.Apply(actor)
            shape = UsdGeom.Cube.Define(stage, path+'/shape').GetPrim(); UsdPhysics.CollisionAPI.Apply(shape)
            a = PhysxSchema.PhysxCollisionAPI.Apply(shape); a.CreateContactOffsetAttr(.02); a.CreateRestOffsetAttr(-.002)
        locked = s._lock_static_contact_shapes(s._static_contact_shapes(stage), 1)
        before = stage.GetRootLayer().ExportToString()
        s._consume_static_contacts(stage, locked, author=True)
        self.assertEqual(stage.GetRootLayer().ExportToString(), before)
        self.assertEqual(len(locked['shapes']), 1)

    def test_bidirectional_set_geometry_ownership_and_source_mutation_rejected_before_any_write(self):
        for change in ('extra', 'missing', 'geometry', 'frame', 'body', 'joint', 'source', 'lock_bits', 'lock_source'):
            stage, prim, locked = self.locked_stage()
            if change == 'extra': UsdPhysics.CollisionAPI.Apply(UsdGeom.Cube.Define(stage, '/World/extra').GetPrim())
            elif change == 'missing': stage.RemovePrim(str(prim.GetPath()))
            elif change == 'geometry': UsdGeom.Cube(prim).GetSizeAttr().Set(2)
            elif change == 'frame': UsdGeom.Xformable(stage.GetPrimAtPath('/World/table')).AddTranslateOp().Set(Gf.Vec3d(.01, 0, 0))
            elif change == 'body': UsdPhysics.RigidBodyAPI.Apply(stage.GetPrimAtPath('/World/table'))
            elif change == 'joint': UsdPhysics.FixedJoint.Define(stage, '/World/joint').CreateBody1Rel().SetTargets(['/World/table'])
            elif change == 'source': PhysxSchema.PhysxCollisionAPI.Apply(prim).CreateContactOffsetAttr(.004)
            elif change == 'lock_bits': locked['shapes'][0]['contact']['bits'] = '00000000'
            else: locked['shapes'][0]['source']['contact'] = {'state': 'finite', 'value': .002, 'authored': True}
            before = stage.GetRootLayer().ExportToString()
            with self.subTest(change=change), self.assertRaises(ValueError): s._consume_static_contacts(stage, locked, author=True)
            self.assertEqual(stage.GetRootLayer().ExportToString(), before)

    def test_writer_geometry_projection_matches_actual_box_cylinder_mesh_and_ground(self):
        world = World(); world.space('room', at=(.2, .3, .4), yaw=.4)
        world.box('table', (1, 1, .1), at=(.1, .2, .3), frame='room', yaw=.2)
        world.cylinder('leg', .02, .4)
        world.box('mesh', (.04, .05, .06), collision_mode='mesh')
        world.box('compound', (1, 1, 1), collision_boxes=[{'size': [.1]*3, 'at': [.3, 0, 0], 'orientation': [1, 0, 0, 0]}])
        with tempfile.TemporaryDirectory() as tmp:
            locked = s.lock_collision_inputs(world, tmp, static_contact_recipe='static-unresolved-contact-v1')
            stage = Usd.Stage.CreateInMemory(); UsdGeom.SetStageMetersPerUnit(stage, 1)
            UsdGeom.Xform.Define(stage, '/World'); ground = UsdGeom.Xform.Define(stage, '/World/ground')
            ground.AddTranslateOp().Set(Gf.Vec3d(0, 0, -.01))
            mesh = s._write_collision_mesh(stage, '/World/ground/visual', s._collision_geometry([s._primitive_visual({'kind':'box','size':[20,20,.02]})]), 'none')
            UsdGeom.Xform.Define(stage, '/World/objects')
            for node in world.nodes.values():
                if node['kind'] == 'space': continue
                path = '/World/objects/o_'+node['id'].encode().hex(); parent = UsdGeom.Xform.Define(stage, path)
                pos, q = world.transform(node['id']); parent.AddTranslateOp().Set(Gf.Vec3d(*pos))
                parent.AddOrientOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Quatd(q[0], Gf.Vec3d(*q[1:])))
                s._write_node_collision(stage, path, node, [s._primitive_visual(node)], Path(tmp), locked)
            s._consume_static_contacts(stage, locked['staticContactRecipe'], author=True)
            before = stage.GetRootLayer().ExportToString(); s._consume_static_contacts(stage, locked['staticContactRecipe'])
            self.assertEqual(stage.GetRootLayer().ExportToString(), before)

    def test_worker_reopen_requires_frozen_native_source_and_never_repairs_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            stage,prim=self.stage()
            PhysxSchema.PhysxCollisionAPI.Apply(prim).CreateContactOffsetAttr(.001)
            model=directory/'scene-source.usda'
            stage.GetRootLayer().Export(str(model))
            audit=worker.audit_native_stage(directory)
            (directory/'native-stage-audit.json').write_text(json.dumps(audit))
            io={'schema':'sceneagent-native-io-v1','clock':{'physicsRateHz':200,'controlRateHz':50},'ports':{},'streams':{}}
            manifest={'runtime':'same','runtimeExecution':{'python':sys.executable,'prefix':sys.prefix},
                'runtimeVersions':worker.runtime_versions(),'assets':{},'io':io,
                'compiled':{'usd':model.name,'nativeStageAudit':'native-stage-audit.json','io':deepcopy(io)},
                'frozenArtifactDigests':{name:worker.digest(directory/name) for name in (model.name,'native-stage-audit.json')}}
            worker.verify_inputs(manifest,'same',directory)
            loaded=Usd.Stage.Open(str(model))
            loaded.GetPrimAtPath('/World/table/collision').GetAttribute('physxCollision:contactOffset').Set(.002)
            loaded.GetRootLayer().Save()
            # Rehashing the root alone cannot rewrite the independent closure receipt.
            manifest['frozenArtifactDigests'][model.name]=worker.digest(model)
            before=model.read_bytes()
            with self.assertRaisesRegex(ValueError,'closure|source'):
                worker.verify_inputs(manifest,'same',directory)
            self.assertEqual(model.read_bytes(),before)

    def test_time_varying_geometry_and_collision_enable_are_not_static_recipe_inputs(self):
        for attr in ('size', 'xform', 'physics:collisionEnabled'):
            stage, prim = self.stage()
            if attr == 'size': UsdGeom.Cube(prim).GetSizeAttr().Set(2., 1.)
            elif attr == 'xform': UsdGeom.Xformable(prim).AddTranslateOp().Set(Gf.Vec3d(1, 0, 0), 1.)
            else: prim.GetAttribute(attr).Set(False, 1.)
            before = stage.GetRootLayer().ExportToString()
            with self.subTest(attr=attr), self.assertRaises(ValueError): s._static_contact_shapes(stage)
            self.assertEqual(stage.GetRootLayer().ExportToString(), before)


@unittest.skipIf(PhysxSchema is None, 'requires the locked native SDK PhysxSchema; exercised on the 4090')
class SingleActorOffsetTests(unittest.TestCase):
    def view(self, contact=(.002, .004), rest=(0, -.001)):
        return SimpleNamespace(check=Mock(return_value=True), count=1, prim_paths=['/World/franka/panda_leftfinger'],
            max_shapes=2, get_contact_offsets=Mock(return_value=np.array([contact], dtype='float32')),
            get_rest_offsets=Mock(return_value=np.array([rest], dtype='float32')))

    def test_public_actor_max_is_permutation_invariant_copied_and_not_qualification(self):
        path = '/World/franka/panda_leftfinger'; view = self.view()
        first = s._single_actor_contact_max(view, path)
        second = s._single_actor_contact_max(self.view((.004, .002), (-.001, 0)), path)
        self.assertEqual(first['maxContactOffsetM'], second['maxContactOffsetM'])
        self.assertFalse(first['qualification'])
        self.assertEqual(first['geometryCoverage'], 'unverified')
        view.get_contact_offsets.return_value[:] = 99
        self.assertLess(first['contactOffsets'][0][0], .01)

    def test_invalid_actor_view_rejects_before_any_offset_getter(self):
        for key, value in [('check', Mock(return_value=False)), ('count', 0), ('count', True),
                           ('count', 2), ('prim_paths', ['/wrong']), ('max_shapes', 0)]:
            view = self.view(); setattr(view, key, value)
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                s._single_actor_contact_max(view, '/World/franka/panda_leftfinger')
            view.get_contact_offsets.assert_not_called(); view.get_rest_offsets.assert_not_called()
        with self.assertRaises(ValueError): s._single_actor_contact_max(None, '/World/franka/panda_leftfinger')

    def test_malformed_nonfinite_or_illegal_rows_and_backend_errors_fail(self):
        for contact, rest in [((0, .004), (0, 0)), ((float('nan'), .004), (0, 0)), ((-.001, .004), (-.002, 0))]:
            with self.subTest(contact=contact), self.assertRaises(ValueError):
                s._single_actor_contact_max(self.view(contact, rest), '/World/franka/panda_leftfinger')
        view = self.view(); view.get_contact_offsets.return_value = np.array([.002, .004])
        with self.assertRaises(ValueError): s._single_actor_contact_max(view, '/World/franka/panda_leftfinger')
        view = self.view(); view.get_contact_offsets.side_effect = RuntimeError('native getter failed')
        with self.assertRaisesRegex(RuntimeError, 'native getter'): s._single_actor_contact_max(view, '/World/franka/panda_leftfinger')


if __name__ == '__main__':
    unittest.main()
