"""SYSTEM_VERIFICATION_ONLY: native preview representation and immutable sources."""
import hashlib
from pathlib import Path
import tempfile
import unittest

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

from runtime.preview_geometry import prepare


class PreviewGeometryTests(unittest.TestCase):
    def test_transformed_native_shapes_keep_bounds_material_and_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source.usda'
            stage = Usd.Stage.CreateNew(str(source))
            root = UsdGeom.Xform.Define(stage, '/World')
            stage.SetDefaultPrim(root.GetPrim())
            root.AddTranslateOp().Set(Gf.Vec3d(2, -3, .5))
            root.AddRotateZOp().Set(37)
            material = UsdShade.Material.Define(stage, '/World/Material')
            expected = {}
            for i, (kind, axis) in enumerate([('Cube', None), ('Cylinder', 'X'), ('Cylinder', 'Y'), ('Cone', 'Z')]):
                path = '/World/object'+str(i)
                shape = getattr(UsdGeom, kind).Define(stage, path)
                if kind == 'Cube':
                    shape.CreateSizeAttr(1.7)
                    size = [.85]*3
                else:
                    shape.CreateHeightAttr(1.2)
                    shape.CreateRadiusAttr(.3)
                    shape.CreateAxisAttr(axis)
                    size = [.3]*3
                    size['XYZ'.index(axis)] = .6
                shape.CreateExtentAttr([Gf.Vec3f(*(-np.array(size))), Gf.Vec3f(*size)])
                UsdGeom.Xformable(shape).AddScaleOp().Set(Gf.Vec3f(.5, 2, 1.3))
                UsdShade.MaterialBindingAPI.Apply(shape.GetPrim()).Bind(material)
                bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default']).ComputeWorldBound(shape.GetPrim()).ComputeAlignedRange()
                expected[path] = np.array([bounds.GetMin(), bounds.GetMax()])
            mesh = UsdGeom.Mesh.Define(stage, '/World/existing_mesh')
            mesh.CreatePointsAttr([Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0)])
            mesh.CreateFaceVertexCountsAttr([3]); mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
            stage.GetRootLayer().Save()
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            target, record = prepare(source, Path(directory)/'derived.usda')
            self.assertEqual(len(record['sourcePrimitives']), 5)
            self.assertEqual(len(record['tessellatedPrimitives']), 4)
            derived = Usd.Stage.Open(str(target))
            for path, bounds in expected.items():
                prim = derived.GetPrimAtPath(path)
                self.assertTrue(prim.IsA(UsdGeom.Mesh))
                actual_bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default']).ComputeWorldBound(prim).ComputeAlignedRange()
                actual = np.array([actual_bounds.GetMin(), actual_bounds.GetMax()])
                np.testing.assert_allclose(actual, bounds, atol=2e-6)
                bound, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
                self.assertEqual(str(bound.GetPath()), '/World/Material')
            self.assertEqual(list(UsdGeom.Mesh(derived.GetPrimAtPath('/World/existing_mesh')).GetFaceVertexIndicesAttr().Get()), [0, 1, 2])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_unsupported_visible_geometry_cannot_be_silently_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source.usda'
            stage = Usd.Stage.CreateNew(str(source))
            stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, '/World').GetPrim())
            UsdGeom.Sphere.Define(stage, '/World/unhandled')
            stage.GetRootLayer().Save()
            with self.assertRaisesRegex(ValueError, 'unsupported visible native preview primitive'):
                prepare(source, Path(directory)/'derived.usda')

    def test_instanced_shape_expands_without_extra_prototype_scene_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory)/'asset.usda'
            stage = Usd.Stage.CreateNew(str(asset))
            stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, '/Asset').GetPrim())
            UsdGeom.Cube.Define(stage, '/Asset/box')
            stage.GetRootLayer().Save()
            source = Path(directory)/'source.usda'
            stage = Usd.Stage.CreateNew(str(source))
            stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, '/World').GetPrim())
            instance = UsdGeom.Xform.Define(stage, '/World/instance')
            instance.GetPrim().GetReferences().AddReference(str(asset))
            instance.GetPrim().SetInstanceable(True)
            instance.AddTranslateOp().Set(Gf.Vec3d(1, 2, 3))
            stage.GetRootLayer().Save()
            target, record = prepare(source, Path(directory)/'derived.usda')
            derived = Usd.Stage.Open(str(target))
            self.assertEqual([str(p.GetPath()) for p in derived.GetPseudoRoot().GetChildren()], ['/World'])
            self.assertTrue(derived.GetPrimAtPath('/World/instance/box').IsA(UsdGeom.Mesh))
            self.assertEqual(record['sourcePrimitives'][0]['path'], '/World/instance/box')


if __name__ == '__main__':
    unittest.main()
