"""SYSTEM_VERIFICATION_ONLY CPU geometry, renderer and sensor doubles; no native execution."""
import unittest


class FrozenIsaacContractsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import numpy as np
            from runtime import simulation
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f'CPU geometry dependency unavailable: {exc.name}')
        cls.np, cls.simulation = np, simulation

    def document(self):
        return {'nodes': [
            {'name': 'World', 'rotation': [-2**-.5, 0, 0, 2**-.5], 'children': [1]},
            {'name': 'World', 'children': [2]},
            {'name': 'franka', 'translation': [.1, .2, .3], 'children': [3]},
            {'name': 'panda_link0', 'children': [4]},
            {'name': 'panda_link0', 'mesh': 0}], 'scenes': [{'nodes': [0]}], 'scene': 0}

    def test_environment_cold_start_initializes_sdk_before_extension_imports(self):
        import builtins
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        original = builtins.__import__
        def cold_import(name, *args, **kwargs):
            if name.startswith('isaacsim.core'):
                raise ModuleNotFoundError("No module named 'isaacsim.core' before SDK startup", name='isaacsim.core')
            return original(name, *args, **kwargs)
        environment = object.__new__(self.simulation.Environment)
        with tempfile.TemporaryDirectory() as name:
            environment.source_directory = Path(name)
            environment.compiled = {}
            with patch('builtins.__import__', side_effect=cold_import), patch.object(
                    self.simulation, '_ensure_app', side_effect=RuntimeError('SDK startup reached')) as start:
                with self.assertRaisesRegex(RuntimeError, 'SDK startup reached'):
                    environment._open_native(False)
                start.assert_called_once_with()

    def test_render_geometry_guard_rejects_another_physics_clock(self):
        from unittest.mock import Mock
        environment = object.__new__(self.simulation.Environment)
        state = {'physicsStep':4,'simulationTime':1.02,'stageId':7}
        environment._clock=Mock(return_value=state)
        environment._validate_geometry(state,[])
        for key in state:
            changed=dict(state);changed[key]+=1
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'clock'):
                environment._validate_geometry(changed,[])






    def test_verified_wrapper_keeps_absolute_world_transform(self):
        paths, worlds = self.simulation._index_nodes(self.document(), ['franka'])
        self.assertEqual(paths['/World/franka/panda_link0'], 3)
        self.assertEqual(paths['/World/franka/panda_link0/panda_link0'], 4)
        self.np.testing.assert_allclose(worlds[3][:3, 3], [.1, .3, -.2], atol=1e-12)

    def test_wrong_wrapper_rotation_and_ambiguous_scene_are_rejected(self):
        for change in ('rotation', 'scene', 'payload', 'namespace'):
            doc = self.document()
            if change == 'rotation':
                doc['nodes'][0]['rotation'] = [0, 0, 0, 1]
            elif change == 'scene':
                doc['scenes'][0]['nodes'] = [0, 2]
            elif change == 'payload':
                doc['nodes'][0]['mesh'] = 0
            else:
                doc['nodes'][1]['children'].append(4)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.simulation._index_nodes(doc, ['franka'])




class ExportedMeshFoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import numpy as np
            from pxr import Gf, Usd, UsdGeom
            from runtime import simulation
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f'CPU USD dependency unavailable: {exc.name}')
        cls.np, cls.Gf, cls.Usd, cls.UsdGeom, cls.simulation = np, Gf, Usd, UsdGeom, simulation

    def fixture(self):
        s, np = self.simulation, self.np
        stage = self.Usd.Stage.CreateInMemory()
        self.UsdGeom.Xform.Define(stage, '/World')
        parent = self.UsdGeom.Xform.Define(stage, '/World/ground')
        parent.AddTranslateOp().Set(self.Gf.Vec3d(0, 0, -.01))
        mesh = self.UsdGeom.Mesh.Define(stage, '/World/ground/visual')
        points = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype='<f4')
        mesh.CreatePointsAttr([self.Gf.Vec3f(*map(float, point)) for point in points])
        mesh.CreateFaceVertexCountsAttr([3]); mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
        document = {'nodes': [{'name': 'World', 'rotation': [-2**-.5, 0, 0, 2**-.5], 'children': [1]},
            {'name': 'World', 'children': [2]}, {'name': 'ground', 'mesh': 0, 'translation': [0, 0, -.01]}],
            'scenes': [{'nodes': [0]}], 'meshes': [{'primitives': [{'attributes': {'POSITION': 0}, 'indices': 1, 'material': 0}]}],
            'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 3, 'type': 'VEC3'},
                {'bufferView': 1, 'componentType': 5125, 'count': 3, 'type': 'SCALAR'}],
            'bufferViews': [{'byteOffset': 0, 'byteLength': 36}, {'byteOffset': 36, 'byteLength': 12}]}
        binary = points.tobytes()+np.array([0, 1, 2], dtype='<u4').tobytes()
        return stage, document, binary

    def test_unique_identity_child_fold_preserves_full_world_transform(self):
        stage, document, binary = self.fixture()
        paths, worlds = self.simulation._index_nodes(document, ['ground'])
        index = self.simulation._preview_mesh_index(stage, '/World/ground/visual', paths, worlds, document, binary)
        self.assertEqual(index, paths['/World/ground'])
        self.np.testing.assert_allclose(worlds[index], self.simulation.TO_GLTF@self.simulation._usd_world(stage, '/World/ground/visual'), atol=1e-12)

    def test_instance_proxy_child_fold_keeps_complete_geometry_and_ambiguity_checks(self):
        from pxr import Sdf
        for ambiguous in (False,True):
            stage,document,binary=self.fixture()
            self.UsdGeom.Xform.Define(stage,'/Library')
            edit=Sdf.BatchNamespaceEdit();edit.Add('/World/ground/visual','/Library/visual')
            self.assertTrue(stage.GetRootLayer().Apply(edit))
            if ambiguous:self.UsdGeom.Mesh.Define(stage,'/Library/other')
            parent=stage.GetPrimAtPath('/World/ground')
            parent.GetReferences().AddInternalReference('/Library');parent.SetInstanceable(True)
            self.assertTrue(stage.GetPrimAtPath('/World/ground/visual').IsInstanceProxy())
            self.assertEqual(parent.GetChildren(),[])
            paths,worlds=self.simulation._index_nodes(document,['ground'])
            if ambiguous:
                with self.assertRaisesRegex(ValueError,'ambiguous'):
                    self.simulation._preview_mesh_index(stage,'/World/ground/visual',paths,worlds,document,binary)
            else:
                self.assertEqual(self.simulation._preview_mesh_index(stage,'/World/ground/visual',paths,worlds,document,binary),
                                 paths['/World/ground'])

    def test_fold_uses_usd_render_purposes_without_removing_collision_geometry(self):
        from pxr import UsdPhysics,UsdRender
        for purpose in ('guide','proxy','render','default'):
            stage,document,binary=self.fixture()
            helper=self.UsdGeom.Mesh.Define(stage,'/World/ground/auxiliary')
            helper.CreatePurposeAttr(purpose);UsdPhysics.CollisionAPI.Apply(helper.GetPrim())
            paths,worlds=self.simulation._index_nodes(document,['ground'])
            default_purposes=UsdRender.Settings.Define(stage,'/Settings').GetIncludedPurposesAttr().Get()
            if purpose in default_purposes:
                with self.subTest(purpose=purpose),self.assertRaisesRegex(ValueError,'ambiguous'):
                    self.simulation._preview_mesh_index(stage,'/World/ground/visual',paths,worlds,document,binary)
            else:
                self.assertEqual(self.simulation._preview_mesh_index(stage,'/World/ground/visual',paths,worlds,document,binary),
                                 paths['/World/ground'])
            self.assertTrue(helper.GetPrim().HasAPI(UsdPhysics.CollisionAPI))
            self.assertEqual(helper.ComputePurpose(),purpose)


    def test_nested_identity_mesh_fold_preserves_source_geometry(self):
        from pxr import Sdf
        stage,document,binary=self.fixture()
        self.UsdGeom.Xform.Define(stage,'/World/ground/visuals')
        edit=Sdf.BatchNamespaceEdit();edit.Add('/World/ground/visual','/World/ground/visuals/mesh')
        self.assertTrue(stage.GetRootLayer().Apply(edit))
        paths,worlds=self.simulation._index_nodes(document,['ground'])
        self.assertEqual(self.simulation._preview_mesh_index(stage,'/World/ground/visuals/mesh',
            paths,worlds,document,binary),paths['/World/ground'])
        self.UsdGeom.Mesh.Define(stage,'/World/ground/visuals/other')
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            self.simulation._preview_mesh_index(stage,'/World/ground/visuals/mesh',paths,worlds,document,binary)

    def branched_fixture(self, nested=False):
        from pxr import Sdf
        stage, document, binary = self.fixture()
        child = self.UsdGeom.Xform.Define(stage, '/World/ground/handle')
        child.AddTranslateOp().Set(self.Gf.Vec3d(.2, 0, 0))
        mesh = self.UsdGeom.Mesh.Define(stage, '/World/ground/handle/visual')
        source = self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/ground/visual'))
        mesh.CreatePointsAttr(source.GetPointsAttr().Get())
        mesh.CreateFaceVertexCountsAttr([3]); mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
        document['nodes'][2]['children'] = [3]
        document['nodes'].append({'name': 'handle', 'mesh': 1, 'translation': [.2, 0, 0]})
        document['meshes'].append({'primitives': [dict(document['meshes'][0]['primitives'][0])]})
        if nested:
            self.UsdGeom.Xform.Define(stage, '/World/ground/geometry')
            edit = Sdf.BatchNamespaceEdit(); edit.Add('/World/ground/visual', '/World/ground/geometry/mesh')
            self.assertTrue(stage.GetRootLayer().Apply(edit))
        return stage, document, binary

    def test_fold_retains_independently_exported_child_meshes(self):
        for nested in (False, True):
            stage, document, binary = self.branched_fixture(nested)
            paths, worlds = self.simulation._index_nodes(document, ['ground'])
            source = '/World/ground/geometry/mesh' if nested else '/World/ground/visual'
            with self.subTest(nested=nested):
                self.assertEqual(self.simulation._preview_mesh_index(stage, source, paths, worlds, document, binary),
                                 paths['/World/ground'])
                self.assertEqual(self.simulation._preview_mesh_index(stage, '/World/ground/handle/visual',
                    paths, worlds, document, binary), paths['/World/ground/handle'])

    def test_branched_fold_still_rejects_missing_sibling_and_wrong_child_geometry_or_pose(self):
        for invalid in ('missing', 'geometry', 'pose', 'unmapped'):
            stage, document, binary = self.branched_fixture()
            if invalid == 'missing':
                document['nodes'][2].pop('children')
            elif invalid == 'geometry':
                mesh = self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/ground/handle/visual'))
                mesh.GetPointsAttr().Set([self.Gf.Vec3f(0, 0, 0), self.Gf.Vec3f(2, 0, 0), self.Gf.Vec3f(0, 1, 0)])
            elif invalid == 'pose':
                document['nodes'][3]['translation'][0] += .01
            else:
                self.UsdGeom.Mesh.Define(stage, '/World/ground/unmapped')
            paths, worlds = self.simulation._index_nodes(document, ['ground'])
            source = '/World/ground/handle/visual' if invalid in ('geometry', 'pose') else '/World/ground/visual'
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.simulation._preview_mesh_index(stage, source, paths, worlds, document, binary)

    def test_exact_mesh_path_also_verifies_complete_geometry_and_world_transform(self):
        for invalid in (None, 'geometry', 'pose'):
            stage, document, binary = self.fixture()
            document['nodes'][2].pop('mesh'); document['nodes'][2]['children'] = [3]
            document['nodes'].append({'name': 'visual', 'mesh': 0})
            if invalid == 'geometry':
                values = self.np.frombuffer(binary[:36], dtype='<f4').copy(); values[0] = .01
                binary = values.tobytes()+binary[36:]
            elif invalid == 'pose':
                document['nodes'][3]['translation'] = [.01, 0, 0]
            paths, worlds = self.simulation._index_nodes(document, ['ground'])
            with self.subTest(invalid=invalid):
                if invalid:
                    with self.assertRaises(ValueError):
                        self.simulation._preview_mesh_index(stage, '/World/ground/visual', paths, worlds, document, binary)
                else:
                    self.assertEqual(self.simulation._preview_mesh_index(stage, '/World/ground/visual',
                        paths, worlds, document, binary), paths['/World/ground/visual'])

    def test_unbound_mesh_preserves_official_default_but_rejects_lost_source_appearance(self):
        from pxr import Sdf, UsdShade
        for appearance in (None, 'bound', 'exported-only', 'color', 'opacity', 'inherited-color'):
            stage, _, _ = self.fixture()
            prim = stage.GetPrimAtPath('/World/ground/visual')
            document = {'materials': [{'name': 'Material'}]}
            material_index = 0 if appearance == 'exported-only' else None
            if appearance == 'bound':
                material = UsdShade.Material.Define(stage, '/World/Material')
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)
            elif appearance == 'color':
                self.UsdGeom.Mesh(prim).CreateDisplayColorAttr([(1, 0, 0)])
            elif appearance == 'opacity':
                self.UsdGeom.Mesh(prim).CreateDisplayOpacityAttr([.5])
            elif appearance == 'inherited-color':
                self.UsdGeom.PrimvarsAPI(stage.GetPrimAtPath('/World/ground')).CreatePrimvar('displayColor',
                    Sdf.ValueTypeNames.Color3fArray, self.UsdGeom.Tokens.constant).Set([(1, 0, 0)])
            with self.subTest(appearance=appearance):
                if appearance:
                    with self.assertRaisesRegex(ValueError, 'material|appearance|opacity'):
                        self.simulation._preview_material_source(stage, prim, material_index, document, b'')
                else:
                    self.assertIsNone(self.simulation._preview_material_source(stage, prim, material_index, document, b''))

    def unbound_color_fixture(self, component=5123, channels=4, inherited=False):
        from pxr import Sdf
        stage, document, binary = self.fixture()
        prim = stage.GetPrimAtPath('/World/ground/visual')
        holder = stage.GetPrimAtPath('/World/ground') if inherited else prim
        self.UsdGeom.PrimvarsAPI(holder).CreatePrimvar('displayColor', Sdf.ValueTypeNames.Color3fArray,
            self.UsdGeom.Tokens.constant).Set([(.22,.46,.58)])
        self.UsdGeom.PrimvarsAPI(holder).CreatePrimvar('displayOpacity', Sdf.ValueTypeNames.FloatArray,
            self.UsdGeom.Tokens.constant).Set([1])
        values = self.np.tile([.22,.46,.58,0][:channels], (3,1))
        dtype, maximum = {5121:('u1',255),5123:('<u2',65535),5126:('<f4',1)}[component]
        encoded = (values*maximum).astype(dtype).tobytes()
        document['accessors'].append({'bufferView':2,'componentType':component,'count':3,
            'type':'VEC'+str(channels),'normalized':component != 5126})
        document['bufferViews'].append({'byteOffset':len(binary),'byteLength':len(encoded)})
        primitive = document['meshes'][0]['primitives'][0]
        primitive.pop('material'); primitive['attributes']['COLOR_0'] = 2
        return stage, prim, document, binary+encoded, primitive

    def test_unbound_constant_color_preserves_normalized_and_float_rgb_with_opaque_defaults(self):
        for component in (5121,5123,5126):
            for channels in (3,4):
                for inherited in (False,True):
                    with self.subTest(component=component,channels=channels,inherited=inherited):
                        stage,prim,document,binary,primitive = self.unbound_color_fixture(component,channels,inherited)
                        source = stage.GetRootLayer().ExportToString()
                        self.assertIsNone(self.simulation._preview_material_source(stage,prim,None,document,binary,primitive))
                        self.assertNotIn('material',primitive)
                        self.assertEqual(stage.GetRootLayer().ExportToString(),source)

    def test_unbound_color_rejects_missing_changed_or_invalid_vertex_encoding(self):
        for invalid in ('missing','rgb','unnormalized','count','nan','opacity'):
            stage,prim,document,binary,primitive = self.unbound_color_fixture(5126 if invalid == 'nan' else 5123)
            if invalid == 'missing':
                primitive['attributes'].pop('COLOR_0')
            elif invalid in ('rgb','nan'):
                dtype = '<f4' if invalid == 'nan' else '<u2'
                begin = document['bufferViews'][2]['byteOffset']
                values = self.np.frombuffer(binary[begin:],dtype=dtype).copy()
                values[0] = self.np.nan if invalid == 'nan' else 65535
                binary = binary[:begin]+values.tobytes()
            elif invalid == 'unnormalized':
                document['accessors'][2]['normalized'] = False
            elif invalid == 'count':
                document['accessors'][2]['count'] = 2
            else:
                self.UsdGeom.Mesh(prim).CreateDisplayOpacityAttr([.5])
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError,'color|appearance|opacity'):
                self.simulation._preview_material_source(stage,prim,None,document,binary,primitive)

    def test_quad_fold_accepts_either_diagonal_but_not_missing_or_changed_geometry(self):
        for indices in ([0,1,2,0,2,3],[0,1,3,1,2,3],[0,1,2],[0,1,2,0,1,3]):
            stage,document,_=self.fixture()
            points=self.np.array([[0,0,0],[1,0,0],[1,1,0],[0,1,0]],dtype='<f4')
            mesh=self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/ground/visual'))
            mesh.GetPointsAttr().Set([self.Gf.Vec3f(*map(float,point)) for point in points])
            mesh.GetFaceVertexCountsAttr().Set([4]);mesh.GetFaceVertexIndicesAttr().Set([0,1,2,3])
            document['accessors'][0]['count']=4;document['accessors'][1]['count']=len(indices)
            document['bufferViews']=[{'byteOffset':0,'byteLength':48},
                {'byteOffset':48,'byteLength':4*len(indices)}]
            binary=points.tobytes()+self.np.array(indices,dtype='<u4').tobytes()
            paths,worlds=self.simulation._index_nodes(document,['ground'])
            if indices in ([0,1,2,0,2,3],[0,1,3,1,2,3]):
                self.assertEqual(self.simulation._preview_mesh_index(stage,'/World/ground/visual',
                    paths,worlds,document,binary),paths['/World/ground'])
            else:
                with self.assertRaises(ValueError):
                    self.simulation._preview_mesh_index(stage,'/World/ground/visual',paths,worlds,document,binary)

    def test_nonplanar_quad_must_preserve_native_fan_surface(self):
        stage, _, _ = self.fixture()
        mesh = self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/ground/visual'))
        points = self.np.array([[0,0,0],[1,0,0],[1,1,.4],[0,1,0]], dtype='<f4')
        mesh.GetPointsAttr().Set([self.Gf.Vec3f(*map(float, point)) for point in points])
        mesh.GetFaceVertexCountsAttr().Set([4]); mesh.GetFaceVertexIndicesAttr().Set([0,1,2,3])
        self.assertTrue(self.simulation._preview_mesh_matches(mesh, points[[[0,1,2],[0,2,3]]]))
        self.assertFalse(self.simulation._preview_mesh_matches(mesh, points[[[0,1,3],[1,2,3]]]))

    def test_tilted_planar_quad_still_accepts_either_diagonal(self):
        stage, _, _ = self.fixture()
        mesh = self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/ground/visual'))
        points = self.np.array([[0,0,0],[1,0,.2],[1,1,.5],[0,1,.3]], dtype='<f4')
        mesh.GetPointsAttr().Set([self.Gf.Vec3f(*map(float, point)) for point in points])
        mesh.GetFaceVertexCountsAttr().Set([4]); mesh.GetFaceVertexIndicesAttr().Set([0,1,2,3])
        for faces in ([[0,1,2],[0,2,3]], [[0,1,3],[1,2,3]]):
            with self.subTest(faces=faces):
                self.assertTrue(self.simulation._preview_mesh_matches(mesh, points[faces]))

    def test_concave_polygon_and_original_face_subsets_keep_complete_surface(self):
        stage,_,_=self.fixture()
        mesh=self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/ground/visual'))
        points=self.np.array([[0,0,0],[2,0,0],[2,2,0],[1,1,0],[0,2,0],[3,0,0],[4,0,0],[3,1,0]],dtype=float)
        mesh.GetPointsAttr().Set([self.Gf.Vec3f(*map(float,point)) for point in points])
        mesh.GetFaceVertexCountsAttr().Set([5,3]);mesh.GetFaceVertexIndicesAttr().Set([0,1,2,3,4,5,6,7])
        polygon=points[[[0,1,3],[1,2,3],[0,3,4]]]
        triangle=points[[[5,6,7]]]
        self.assertTrue(self.simulation._preview_mesh_matches(mesh,polygon,[0]))
        self.assertTrue(self.simulation._preview_mesh_matches(mesh,triangle,[1]))
        self.assertTrue(self.simulation._preview_mesh_matches(mesh,self.np.concatenate([polygon,triangle])))
        self.assertFalse(self.simulation._preview_mesh_matches(mesh,polygon,[1]))
        self.assertTrue(self.simulation._preview_mesh_matches(mesh,points[[[0,1,2],[0,2,3],[0,3,4]]],[0]))
        self.assertFalse(self.simulation._preview_mesh_matches(mesh,points[[[0,1,2],[0,2,3],[0,3,3]]],[0]))
        changed=polygon.copy();changed[0,0,2]=.001
        self.assertFalse(self.simulation._preview_mesh_matches(mesh,changed,[0]))
        with self.assertRaises(ValueError):
            self.simulation._preview_mesh_matches(mesh,triangle,[-1])

    def test_native_fan_retains_all_positive_and_degenerate_triangles_at_collapsed_edges(self):
        stage, _, _ = self.fixture()
        mesh = self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/ground/visual'))
        points = self.np.array([[0,0,0],[0,0,0],[1,0,0],[0,1,0],[-1,0,0]], dtype=float)
        mesh.GetPointsAttr().Set([self.Gf.Vec3f(*map(float, point)) for point in points])
        mesh.GetFaceVertexCountsAttr().Set([4,4]); mesh.GetFaceVertexIndicesAttr().Set([0,1,2,3,0,1,3,4])
        fan = points[[[0,1,2],[0,2,3],[0,1,3],[0,3,4]]]
        self.assertTrue(self.simulation._preview_mesh_matches(mesh, fan))
        self.assertTrue(self.simulation._preview_mesh_matches(mesh, points[[[0,1,2],[0,2,3]]], [0]))
        for faces in ([[0,2,3],[0,1,3],[0,3,4]],
                      [[0,1,2],[0,2,3],[0,1,2],[0,3,4]],
                      [[2,2,3],[0,2,3],[0,1,3],[0,3,4]],
                      [[0,1,3],[0,2,3],[0,1,3],[0,2,3]]):
            with self.subTest(invalid_faces=faces):
                self.assertFalse(self.simulation._preview_mesh_matches(mesh, points[faces]))
        changed = fan.copy(); changed[0,0,2] = .001
        self.assertFalse(self.simulation._preview_mesh_matches(mesh, changed))

    def test_native_concave_fan_must_retain_every_triangle_and_coordinate(self):
        stage, _, _ = self.fixture()
        mesh = self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/ground/visual'))
        points = self.np.array([[.7,0,0],[1.2,0,0],[1.2,.4,0],[.9,.15,0],[.7,.4,0]], dtype='<f4')
        mesh.GetPointsAttr().Set([self.Gf.Vec3f(*map(float, point)) for point in points])
        mesh.GetFaceVertexCountsAttr().Set([5]); mesh.GetFaceVertexIndicesAttr().Set([0,1,2,3,4])
        fan = points[[[0,1,2],[0,2,3],[0,3,4]]]
        # The native fan is the frozen renderer's surface even when its absolute
        # triangle area differs from the ideal concave polygon boundary area.
        self.assertTrue(self.simulation._preview_mesh_matches(mesh, fan))
        for missing in range(3):
            with self.subTest(missing=missing):
                self.assertFalse(self.simulation._preview_mesh_matches(mesh, self.np.delete(fan, missing, axis=0)))
        self.assertFalse(self.simulation._preview_mesh_matches(mesh, self.np.concatenate([fan, fan[:1]])))
        changed = fan.copy(); changed[1,1,2] += .001
        self.assertFalse(self.simulation._preview_mesh_matches(mesh, changed))

    def test_ordinary_polygon_cannot_gain_an_unauthored_degenerate_triangle(self):
        stage, _, _ = self.fixture()
        mesh = self.UsdGeom.Mesh(stage.GetPrimAtPath('/World/ground/visual'))
        points = self.np.array([[0,0,0],[1,0,0],[1,1,0],[0,1,0]], dtype=float)
        mesh.GetPointsAttr().Set([self.Gf.Vec3f(*map(float, point)) for point in points])
        mesh.GetFaceVertexCountsAttr().Set([4]); mesh.GetFaceVertexIndicesAttr().Set([0,1,2,3])
        self.assertFalse(self.simulation._preview_mesh_matches(mesh, points[[[0,0,1],[0,1,2],[0,2,3]]]))

    def test_actual_small_quatf_angle_reproduces_original_usd_matrix_loss(self):
        stage = self.Usd.Stage.CreateInMemory()
        xform = self.UsdGeom.Xform.Define(stage, '/World/link')
        angle = 6.599563494e-5
        q = self.Gf.Quatf(float(self.np.cos(angle/2)), self.Gf.Vec3f(0, 0, float(self.np.sin(angle/2))))
        xform.AddOrientOp().Set(q)
        self.assertEqual(q.GetReal(), 1.)
        raw = self.simulation._usd_world(stage, '/World/link')
        self.np.testing.assert_array_equal(raw, self.np.eye(4))
        expected = self.simulation._pose([0, 0, 0], [q.GetReal(), *q.GetImaginary()])
        self.assertAlmostEqual(float(self.np.max(self.np.abs(raw-expected))), 6.59956348690588e-5, places=15)





    def test_fold_rejects_wrong_matrix_geometry_ambiguous_source_and_nonidentity_child(self):
        for change in ('matrix', 'geometry', 'sibling', 'child_transform'):
            stage, document, binary = self.fixture()
            if change == 'matrix':
                document['nodes'][2]['translation'][0] = .01
            elif change == 'geometry':
                values = self.np.frombuffer(binary[:36], dtype='<f4').copy(); values[0] = .01
                binary = values.tobytes()+binary[36:]
            elif change == 'sibling':
                self.UsdGeom.Mesh.Define(stage, '/World/ground/other')
            else:
                self.UsdGeom.Xformable(stage.GetPrimAtPath('/World/ground/visual')).AddTranslateOp().Set(self.Gf.Vec3d(.01, 0, 0))
            paths, worlds = self.simulation._index_nodes(document, ['ground'])
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.simulation._preview_mesh_index(stage, '/World/ground/visual', paths, worlds, document, binary)


class PreviewShaderInputTests(unittest.TestCase):
    def fixture(self):
        from pxr import Sdf,Usd,UsdShade
        from runtime import simulation
        stage=Usd.Stage.CreateInMemory();material=UsdShade.Material.Define(stage,'/World/Material')
        shader=UsdShade.Shader.Define(stage,'/World/Material/Surface');shader.CreateIdAttr('UsdPreviewSurface')
        material.CreateSurfaceOutput().ConnectToSource(shader.CreateOutput('surface',Sdf.ValueTypeNames.Token))
        return simulation,Sdf,stage,material,shader

    def test_material_interface_values_override_shader_fallback_and_missing_inputs_use_sdr(self):
        sim,Sdf,stage,material,shader=self.fixture()
        for name,type_,value in [('diffuseColor',Sdf.ValueTypeNames.Color3f,(.2,.3,.4)),
                ('opacity',Sdf.ValueTypeNames.Float,.6),('roughness',Sdf.ValueTypeNames.Float,.7),
                ('metallic',Sdf.ValueTypeNames.Float,.15)]:
            interface=material.CreateInput(name,type_);interface.Set(value)
            input_=shader.CreateInput(name,type_);input_.Set((.8,.8,.8) if name=='diffuseColor' else .9)
            input_.ConnectToSource(interface)
        target={'name':'Material','pbrMetallicRoughness':{'baseColorFactor':[.2,.3,.4,.6],
            'roughnessFactor':.7,'metallicFactor':.15}}
        source=sim._recover_material(stage,material,target,{},b'')
        self.assertAlmostEqual(source['baseColor'][0],.2,places=7)
        self.assertAlmostEqual(source['baseColor'][3],.6,places=7)
        self.assertEqual(source['emissive'],[0,0,0])
        self.assertEqual(target['alphaMode'],'BLEND')

    def test_unprovided_preview_inputs_are_read_from_the_registered_sdk_definition(self):
        from pxr import Sdr
        sim,Sdf,stage,material,shader=self.fixture();target={'name':'Material'}
        source=sim._recover_material(stage,material,target,{},b'')
        definition=Sdr.Registry().GetShaderNodeByIdentifier('UsdPreviewSurface')
        self.assertEqual(source['baseColor'],[*definition.GetShaderInput('diffuseColor').GetDefaultValue(),
            definition.GetShaderInput('opacity').GetDefaultValue()])
        self.assertEqual(source['roughness'],definition.GetShaderInput('roughness').GetDefaultValue())

    def test_ambiguous_interface_or_explicit_block_is_not_replaced_with_a_default(self):
        for invalid in ('multiple','blocked','dangling'):
            sim,Sdf,stage,material,shader=self.fixture()
            input_=shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f)
            if invalid=='blocked':input_.GetAttr().Block()
            elif invalid=='dangling':input_.GetAttr().SetConnections(['/World/Material.inputs:missing'])
            else:
                first=material.CreateInput('first',Sdf.ValueTypeNames.Color3f);first.Set((.1,.2,.3))
                other=material.CreateInput('other',Sdf.ValueTypeNames.Color3f);other.Set((.3,.2,.1))
                input_.GetAttr().SetConnections([first.GetAttr().GetPath(),other.GetAttr().GetPath()])
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                sim._recover_material(stage,material,{'name':'Material'},{},b'')

    def test_resolved_texture_output_keeps_existing_channel_pixel_and_factor_checks(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        from pxr import UsdShade
        sim,Sdf,stage,material,shader=self.fixture()
        texture=UsdShade.Shader.Define(stage,'/World/Material/Texture');texture.CreateIdAttr('UsdUVTexture')
        shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).ConnectToSource(texture.CreateOutput('rgb',Sdf.ValueTypeNames.Float3))
        texture.CreateInput('sourceColorSpace',Sdf.ValueTypeNames.Token).Set('sRGB')
        texture.CreateInput('scale',Sdf.ValueTypeNames.Float4).Set((.5,.5,.5,1))
        texture.CreateInput('bias',Sdf.ValueTypeNames.Float4).Set((0,0,0,0))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'color.png';Image.new('RGBA',(1,1),(64,128,255,128)).save(path)
            texture.CreateInput('file',Sdf.ValueTypeNames.Asset).Set(str(path));binary=path.read_bytes()
            document={'textures':[{'source':0}],'images':[{'bufferView':0}],
                'bufferViews':[{'byteOffset':0,'byteLength':len(binary)}]}
            target={'name':'Material','pbrMetallicRoughness':{'baseColorTexture':{'index':0}}}
            from unittest.mock import patch
            from pxr import Sdr
            with patch.object(Sdr,'Registry',side_effect=AssertionError('connected output must not query Sdr defaults')):
                value,output=sim._resolved_shader_input(shader,'diffuseColor')
                self.assertIsNone(value);self.assertEqual(output.GetBaseName(),'rgb')
            sim._recover_material(stage,material,target,document,binary)
            self.assertEqual(target['pbrMetallicRoughness']['baseColorFactor'],[.5,.5,.5,1])
            texture.CreateIdAttr('UnknownShader')
            with self.assertRaisesRegex(ValueError,'unsupported effective texture'):
                sim._recover_material(stage,material,target,document,binary)

    def test_texture_scale_and_bias_use_effective_interface_values_or_sdk_defaults(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        from pxr import Sdr,UsdShade
        definition=Sdr.Registry().GetShaderNodeByIdentifier('UsdUVTexture')
        for mode in ('defaults','interface'):
            sim,Sdf,stage,material,shader=self.fixture()
            texture=UsdShade.Shader.Define(stage,'/World/Material/Texture');texture.CreateIdAttr('UsdUVTexture')
            shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).ConnectToSource(texture.CreateOutput('rgb',Sdf.ValueTypeNames.Float3))
            texture.CreateInput('sourceColorSpace',Sdf.ValueTypeNames.Token).Set('sRGB')
            expected=list(definition.GetShaderInput('scale').GetDefaultValue())
            if mode=='interface':
                expected=[.25,.5,.75,1]
                for name,value in [('scale',expected),('bias',[0,0,0,0])]:
                    interface=material.CreateInput(name,Sdf.ValueTypeNames.Float4);interface.Set(tuple(value))
                    texture.CreateInput(name,Sdf.ValueTypeNames.Float4).ConnectToSource(interface)
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'color.png';Image.new('RGBA',(1,1),(64,128,255,255)).save(path)
                texture.CreateInput('file',Sdf.ValueTypeNames.Asset).Set(str(path));binary=path.read_bytes()
                document={'textures':[{'source':0}],'images':[{'bufferView':0}],
                    'bufferViews':[{'byteOffset':0,'byteLength':len(binary)}]}
                target={'name':'Material','pbrMetallicRoughness':{'baseColorTexture':{'index':0}}}
                sim._recover_material(stage,material,target,document,binary)
                self.assertEqual(target['pbrMetallicRoughness']['baseColorFactor'],[*expected[:3],1])
                texture.GetInput('sourceColorSpace').Set('raw')
                with self.assertRaisesRegex(ValueError,'unsupported effective texture'):
                    sim._recover_material(stage,material,target,document,binary)

    def texture_fixture(self,directory,data,color_space=None):
        from pathlib import Path
        from pxr import UsdShade
        sim,Sdf,stage,material,shader=self.fixture()
        texture=UsdShade.Shader.Define(stage,'/World/Material/Texture');texture.CreateIdAttr('UsdUVTexture')
        shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).ConnectToSource(texture.CreateOutput('rgb',Sdf.ValueTypeNames.Float3))
        path=Path(directory)/'texture.png';path.write_bytes(data)
        texture.CreateInput('file',Sdf.ValueTypeNames.Asset).Set(str(path))
        if color_space is not None:texture.CreateInput('sourceColorSpace',Sdf.ValueTypeNames.Token).Set(color_space)
        document={'textures':[{'source':0}],'images':[{'bufferView':0}],
            'bufferViews':[{'byteOffset':0,'byteLength':len(data)}]}
        target={'pbrMetallicRoughness':{'baseColorTexture':{'index':0}}}
        return sim,Sdf,stage,material,texture,target,document

    def test_auto_texture_colorspace_uses_image_content_and_registered_default(self):
        from io import BytesIO
        import tempfile
        from PIL import Image
        for format_,mode in [('JPEG','RGB'),('PNG','RGB'),('PNG','RGBA')]:
            encoded=BytesIO();Image.new(mode,(2,2),(64,128,255) if mode=='RGB' else (64,128,255,128)).save(encoded,format=format_)
            data=encoded.getvalue()
            for color_space in (None,'auto','interface-auto','interface-sRGB'):
                with self.subTest(format=format_,mode=mode,color_space=color_space),tempfile.TemporaryDirectory() as directory:
                    sim,Sdf,stage,material,texture,target,document=self.texture_fixture(directory,data)
                    if color_space and color_space.startswith('interface-'):
                        interface=material.CreateInput('textureColorSpace',Sdf.ValueTypeNames.Token)
                        interface.Set(color_space.removeprefix('interface-'))
                        texture.CreateInput('sourceColorSpace',Sdf.ValueTypeNames.Token).ConnectToSource(interface)
                        file=material.CreateInput('textureFile',Sdf.ValueTypeNames.Asset);file.Set(texture.GetInput('file').Get())
                        texture.GetInput('file').ConnectToSource(file);texture.GetInput('file').GetAttr().Clear()
                    elif color_space:texture.CreateInput('sourceColorSpace',Sdf.ValueTypeNames.Token).Set(color_space)
                    source_before=stage.GetRootLayer().ExportToString()
                    sim._recover_material(stage,material,target,document,data)
                    self.assertEqual(target['pbrMetallicRoughness']['baseColorFactor'],[1,1,1,1])
                    self.assertEqual(stage.GetRootLayer().ExportToString(),source_before)

    def test_auto_texture_colorspace_does_not_guess_unverified_encoding_or_metadata(self):
        from io import BytesIO
        import struct,tempfile,zlib
        from PIL import Image,PngImagePlugin
        def png_chunk(kind,data):
            return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
        rgb16=(b'\x89PNG\r\n\x1a\n'+png_chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,16,2,0,0,0))+
            png_chunk(b'IDAT',zlib.compress(b'\x00\x40\x00\x80\x00\xff\xff'))+png_chunk(b'IEND',b''))
        cases=[('rgb16',rgb16)]
        for label,mode,options in [('gray','L',{}),('palette','P',{}),('icc','RGB',{'icc_profile':b'unverified profile'})]:
            encoded=BytesIO();Image.new(mode,(1,1)).save(encoded,format='PNG',**options);cases.append((label,encoded.getvalue()))
        gamma=PngImagePlugin.PngInfo();gamma.add(b'gAMA',struct.pack('>I',100000))
        encoded=BytesIO();Image.new('RGB',(1,1)).save(encoded,format='PNG',pnginfo=gamma);cases.append(('linear-gamma',encoded.getvalue()))
        for label,data in cases:
            with self.subTest(label=label),tempfile.TemporaryDirectory() as directory:
                sim,_,stage,material,texture,target,document=self.texture_fixture(directory,data,'auto')
                with self.assertRaisesRegex(ValueError,'unsupported effective texture'):
                    sim._recover_material(stage,material,target,document,data)
                texture.GetInput('sourceColorSpace').Set('sRGB')
                sim._recover_material(stage,material,target,document,data)

    def test_auto_texture_recovery_keeps_pixel_and_effective_input_guards(self):
        from io import BytesIO
        import tempfile
        from PIL import Image
        encoded=BytesIO();Image.new('RGB',(1,1),(64,128,255)).save(encoded,format='PNG');data=encoded.getvalue()
        for invalid in ('pixels','channel','shader','blocked-color','dangling-color','output-color',
                        'blocked-file','dangling-file','multiple-file','output-file'):
            with self.subTest(invalid=invalid),tempfile.TemporaryDirectory() as directory:
                from pxr import UsdShade
                sim,Sdf,stage,material,texture,target,document=self.texture_fixture(directory,data,'auto')
                binary=data
                if invalid=='pixels':
                    changed=BytesIO();Image.new('RGB',(1,1),(1,2,3)).save(changed,format='PNG');binary=changed.getvalue()
                    document['bufferViews'][0]['byteLength']=len(binary)
                elif invalid=='channel':
                    shader=UsdShade.Shader(stage.GetPrimAtPath('/World/Material/Surface'))
                    shader.GetInput('diffuseColor').ConnectToSource(texture.CreateOutput('r',Sdf.ValueTypeNames.Float))
                elif invalid=='shader':texture.CreateIdAttr('UnknownShader')
                elif invalid in ('blocked-color','blocked-file'):
                    texture.GetInput('sourceColorSpace' if invalid=='blocked-color' else 'file').GetAttr().Block()
                elif invalid in ('dangling-color','dangling-file'):
                    input_=texture.GetInput('sourceColorSpace' if invalid=='dangling-color' else 'file')
                    input_.GetAttr().Clear();input_.GetAttr().SetConnections(['/World/Material.inputs:missing'])
                elif invalid=='multiple-file':
                    first=material.CreateInput('first',Sdf.ValueTypeNames.Asset);first.Set(texture.GetInput('file').Get())
                    second=material.CreateInput('second',Sdf.ValueTypeNames.Asset);second.Set(texture.GetInput('file').Get())
                    texture.GetInput('file').GetAttr().SetConnections([first.GetAttr().GetPath(),second.GetAttr().GetPath()])
                else:
                    name,type_=('sourceColorSpace',Sdf.ValueTypeNames.Token) if invalid=='output-color' else ('file',Sdf.ValueTypeNames.Asset)
                    source=UsdShade.Shader.Define(stage,'/World/Material/Source')
                    texture.GetInput(name).ConnectToSource(source.CreateOutput('value',type_))
                with self.assertRaises(ValueError):sim._recover_material(stage,material,target,document,binary)

    def test_texture_scale_and_bias_do_not_replace_invalid_connections_with_defaults(self):
        from pxr import UsdShade
        for name in ('scale','bias'):
            for invalid in ('blocked','dangling','multiple','output'):
                sim,Sdf,stage,material,shader=self.fixture()
                texture=UsdShade.Shader.Define(stage,'/World/Material/Texture');texture.CreateIdAttr('UsdUVTexture')
                shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).ConnectToSource(texture.CreateOutput('rgb',Sdf.ValueTypeNames.Float3))
                texture.CreateInput('sourceColorSpace',Sdf.ValueTypeNames.Token).Set('sRGB')
                input_=texture.CreateInput(name,Sdf.ValueTypeNames.Float4)
                if invalid=='blocked':input_.GetAttr().Block()
                elif invalid=='dangling':input_.GetAttr().SetConnections(['/World/Material.inputs:missing'])
                elif invalid=='multiple':
                    first=material.CreateInput('first',Sdf.ValueTypeNames.Float4);first.Set((1,1,1,1))
                    other=material.CreateInput('other',Sdf.ValueTypeNames.Float4);other.Set((1,1,1,1))
                    input_.GetAttr().SetConnections([first.GetAttr().GetPath(),other.GetAttr().GetPath()])
                else:
                    source=UsdShade.Shader.Define(stage,'/World/Material/Source')
                    input_.ConnectToSource(source.CreateOutput('value',Sdf.ValueTypeNames.Float4))
                with self.subTest(name=name,invalid=invalid),self.assertRaises(ValueError):
                    sim._recover_material(stage,material,{'name':'Material'},{},b'')




if __name__ == '__main__':
    unittest.main()
