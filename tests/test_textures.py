import json
import math
from pathlib import Path
import tempfile
import unittest

from runtime.scene import World


class MaterialContractsTests(unittest.TestCase):
    def test_material_assets_have_no_implicit_gray_tint(self):
        world = World()
        world.asset('asset', '/prepared/visual.glb', (1, 1, 1), 'Z')
        world.box('painted', (1, 1, 1), material='/prepared/report.json')
        self.assertEqual(world.nodes['asset']['color'], [1, 1, 1, 1])
        self.assertEqual(world.nodes['painted']['color'], [1, 1, 1, 1])

    def test_material_references_and_uv_repeats_are_validated(self):
        for fields in ({'material': {}}, {'material': ''}, {'uv_repeat': (0, 1)},
                       {'uv_repeat': (1, float('nan'))}, {'uv_repeat': (1, 2, 3)}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                World().box('box', (1, 1, 1), **fields)


class TextureRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import numpy as np
            import trimesh
            from PIL import Image
            from runtime import simulation
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f'CPU material dependency unavailable: {exc.name}')
        cls.np, cls.trimesh, cls.Image, cls.simulation = np, trimesh, Image, simulation

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.report = self.directory / 'report.json'
        maps = {}
        for name, color in [('baseColor', (220, 40, 20)), ('normal', (128, 128, 255)),
                            ('metallicRoughness', (0, 140, 70)), ('occlusion', (80, 80, 80)),
                            ('emissive', (20, 35, 50))]:
            self.Image.new('RGB', (8, 8), color).save(self.directory / (name+'.png'))
            maps[name] = name+'.png'
        self.report.write_text(json.dumps({'kind': 'material', 'maps': maps,
            'factors': {'baseColor': [1, 1, 1, .6], 'roughness': .45, 'metallic': .25,
                        'emissive': [.2, .3, .4]}, 'emissiveStrength': 3,
            'alphaMode': 'MASK', 'alphaCutoff': .4, 'doubleSided': True}))

    def test_complete_material_maps_and_emission_are_retained(self):
        material = self.simulation._material(self.report)
        self.assertIsNotNone(material.occlusionTexture)
        self.assertIsNotNone(material.emissiveTexture)
        self.np.testing.assert_allclose(material.emissiveFactor, [.2, .3, .4])
        self.assertEqual(material.emissiveStrength, 3)
        self.assertEqual(material.alphaMode, 'MASK')
        self.assertEqual(material.alphaCutoff, .4)
        self.assertTrue(material.doubleSided)

    def test_untextured_primitive_default_white_uses_normalized_color_units(self):
        mesh = self.simulation._primitive_visual({'kind': 'box', 'size': [1, 1, 1]})
        self.np.testing.assert_allclose(self.trimesh.visual.color.to_float(mesh.visual.material.baseColorFactor), [1, 1, 1, 1])

    def test_prepared_material_default_and_integer_factors_use_normalized_units(self):
        for factors, expected in [({}, [1, 1, 1, 1]), ({'baseColor': [1, 0, 0, 1]}, [1, 0, 0, 1])]:
            self.report.write_text(json.dumps({'kind': 'material', 'factors': factors}))
            material = self.simulation._material(self.report)
            self.np.testing.assert_allclose(self.trimesh.visual.color.to_float(material.baseColorFactor), expected)

    def test_thumbnail_cold_start_initializes_sdk_before_usd_import(self):
        import builtins
        from unittest.mock import patch
        original = builtins.__import__
        def cold_import(name, *args, **kwargs):
            if name == 'pxr':
                raise ModuleNotFoundError("No module named 'pxr' before SDK startup", name='pxr')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=cold_import), patch.object(
                self.simulation, '_ensure_app', side_effect=RuntimeError('SDK startup reached')) as start:
            with self.assertRaisesRegex(RuntimeError, 'SDK startup reached'):
                self.simulation.render_asset_thumbnail({}, self.directory)
            start.assert_called_once_with()

    def test_material_paths_cannot_escape_package(self):
        self.report.write_text(json.dumps({'kind': 'material', 'maps': {'baseColor': '../outside.png'}}))
        with self.assertRaisesRegex(ValueError, 'inside the prepared directory'):
            self.simulation._material(self.report)

    def test_metre_period_preserves_same_scale_on_thin_box_faces(self):
        node = {'kind': 'box', 'size': [.4, .3, .02], 'material': str(self.report),
                'texture_period_m': [.1, .1]}
        mesh = self.simulation._primitive_visual(node)
        for face, normal in zip(mesh.faces, mesh.face_normals):
            axes = [axis for axis in range(3) if axis != int(self.np.argmax(self.np.abs(normal)))]
            self.np.testing.assert_allclose(self.np.ptp(mesh.visual.uv[face], axis=0),
                self.np.ptp(mesh.vertices[face][:, axes], axis=0)/.1, atol=1e-12)

    def test_cylinder_metre_period_and_side_seam(self):
        node = {'kind': 'cylinder', 'size': [.2, .2, .3], 'material': str(self.report),
                'texture_period_m': [.1, .1]}
        mesh = self.simulation._primitive_visual(node)
        sides = [mesh.visual.uv[face] for face, normal in zip(mesh.faces, mesh.face_normals) if abs(normal[2]) < .5]
        self.assertLessEqual(max(self.np.ptp(uv[:, 0]) for uv in sides), math.pi*.2/.1/32+1e-6)
        self.assertAlmostEqual(min(uv[:, 1].min() for uv in sides), 0)
        self.assertAlmostEqual(max(uv[:, 1].max() for uv in sides), 3)

    def test_uniform_fit_is_idempotent_and_preserves_child_transform(self):
        source = self.directory / 'visual.glb'
        scene = self.trimesh.Scene()
        mesh = self.trimesh.creation.box(extents=[1, 2, 3])
        scene.add_geometry(mesh, transform=self.trimesh.transformations.translation_matrix([2, -.7, .5]))
        scene.export(source)
        world = World()
        world.asset('cabinet', source, (4, 4, 4), 'Z')
        self.simulation.normalize_assets(world)
        node = world.nodes['cabinet']
        self.np.testing.assert_allclose(node['size'], [4/3, 8/3, 4])
        parts = self.simulation._asset_parts(node)
        self.np.testing.assert_allclose(self.np.vstack([part.vertices for part in parts]).mean(axis=0), 0, atol=1e-8)
        first = world.snapshot()
        self.simulation.normalize_assets(world)
        self.assertEqual(first, world.snapshot())

    def test_build_material_validation_retains_its_referenced_sampler_scope(self):
        from unittest.mock import patch
        source = self.directory / 'source.glb'
        document = {'asset': {'version': '2.0'}, 'meshes': [{'primitives': []}],
                    'samplers': [{'wrapS': 33071}], 'textures': [{'sampler': 0}], 'materials': [{}]}
        node = {'path': source, 'up_axis': 'Z'}
        self.simulation._write_glb(source, document, b'')
        with patch.object(self.trimesh, 'load', return_value='loaded') as load:
            self.assertEqual(self.simulation._asset_scene(node), 'loaded')
            load.assert_called_once()
        document['materials'][0]['normalTexture'] = {'index': 0}
        self.simulation._write_glb(source, document, b'')
        with patch.object(self.trimesh, 'load', side_effect=AssertionError('unsupported sampler reached loader')):
            with self.assertRaisesRegex(ValueError, 'sampler|wrap'):
                self.simulation._asset_scene(node)

    def test_build_rejects_source_material_controls_before_loading(self):
        from unittest.mock import patch
        source = self.directory / 'source.glb'
        for material in ({'normalTexture': {'index': 0, 'texCoord': 1}},
                         {'normalTexture': {'index': 0, 'scale': .5}},
                         {'extensions': {'KHR_materials_emissive_strength': {'emissiveStrength': 2}}}):
            with self.subTest(material=material):
                self.simulation._write_glb(source, {'asset': {'version': '2.0'}, 'meshes': [{'primitives': []}],
                                                  'textures': [{}], 'materials': [material]}, b'')
                with patch.object(self.trimesh, 'load', side_effect=AssertionError('unsupported material reached loader')):
                    with self.assertRaisesRegex(ValueError, 'mapping|emission strength'):
                        self.simulation._asset_scene({'path': source, 'up_axis': 'Z'})

    def test_unknown_nondefault_material_map_is_rejected(self):
        report = json.loads(self.report.read_text())
        report['maps']['clearcoat'] = 'baseColor.png'
        self.report.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'unsupported material map'):
            self.simulation._material(self.report)

    def test_opaque_source_alpha_is_not_turned_into_transparency(self):
        mesh = self.trimesh.creation.box()
        mesh.visual = self.trimesh.visual.TextureVisuals(material=self.trimesh.visual.material.PBRMaterial(
            baseColorFactor=[1., 1., 1., .4], alphaMode='OPAQUE'))
        result = self.simulation._tint(mesh, [1, 1, 1, 1])
        self.assertEqual(result.visual.material.alphaMode, 'OPAQUE')
        self.assertEqual(result.visual.material.baseColorFactor[3], 255)
        self.assertEqual(self.simulation._tint(result, [1, 1, 1, .6]).visual.material.alphaMode, 'BLEND')

    def test_auto_icc_checks_encoding_instead_of_profile_name(self):
        from io import BytesIO
        from PIL import ImageCms
        import struct
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        tags = {signature: offset for signature, offset, _ in (struct.unpack_from('>4sII', profile, 132+i*12)
            for i in range(struct.unpack_from('>I', profile, 128)[0]))}
        legacy = bytearray(profile)
        for tag, values in [(b'rXYZ', [.43606567, .22248840, .01391602]),
                (b'gXYZ', [.38514709, .71687317, .09707642]),
                (b'bXYZ', [.14306641, .06060791, .71409607])]:
            struct.pack_into('>iii', legacy, tags[tag]+8, *(round(value*65536) for value in values))
        wrong_curve = bytearray(profile)
        struct.pack_into('>i', wrong_curve, tags[b'rTRC']+12, 65536)
        wrong_primaries = bytearray(profile)
        struct.pack_into('>i', wrong_primaries, tags[b'rXYZ']+8, 32768)
        for label, data, expected in [('sRGB', profile, True), ('legacy-sRGB', legacy, True),
                ('different-curve', wrong_curve, False), ('different-primaries', wrong_primaries, False),
                ('malformed', b'unknown profile', False)]:
            encoded = BytesIO()
            self.Image.new('RGB', (2, 2), (64, 128, 230)).save(encoded, format='JPEG', icc_profile=bytes(data))
            with self.subTest(profile=label), self.Image.open(BytesIO(encoded.getvalue())) as image:
                self.assertEqual(self.simulation._auto_texture_is_srgb(image, encoded.getvalue()), expected)

    def test_auto_png_metadata_and_bit_depth_are_checked(self):
        from io import BytesIO
        from PIL import ImageCms, PngImagePlugin
        import struct
        srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        for label, mode, chunks, profile, expected in [
                ('srgb-gamma', 'RGB', [(b'gAMA', struct.pack('>I', 45455))], None, True),
                ('linear-gamma', 'RGB', [(b'gAMA', struct.pack('>I', 100000))], None, False),
                ('srgb-chromaticities', 'RGB', [(b'cHRM', struct.pack('>8I',
                    31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000))], None, True),
                ('different-chromaticities', 'RGB', [(b'cHRM', struct.pack('>8I',
                    31270, 32900, 68000, 32000, 26500, 69000, 15000, 6000))], None, False),
                ('srgb-icc', 'RGB', [], srgb, True),
                ('malformed-icc', 'RGB', [], b'unknown profile', False),
                ('grayscale', 'L', [], None, False),
                ('sixteen-bit', 'I;16', [], None, False)]:
            metadata = PngImagePlugin.PngInfo()
            for kind, data in chunks:
                metadata.add(kind, data)
            encoded = BytesIO()
            self.Image.new(mode, (2, 2)).save(encoded, format='PNG', pnginfo=metadata, icc_profile=profile)
            with self.subTest(metadata=label), self.Image.open(BytesIO(encoded.getvalue())) as image:
                self.assertEqual(self.simulation._auto_texture_is_srgb(image, encoded.getvalue()), expected)


class UsdOpacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import trimesh
            from PIL import Image
            from pxr import Usd, UsdShade
            from runtime import simulation
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f'CPU USD dependency unavailable: {exc.name}')
        cls.trimesh, cls.Image, cls.Usd, cls.UsdShade, cls.simulation = trimesh, Image, Usd, UsdShade, simulation

    def author(self, mode, cutoff=.5):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        stage = self.Usd.Stage.CreateNew(str(directory/'scene.usda'))
        material = self.trimesh.visual.material.PBRMaterial(baseColorFactor=[1., 1., 1., .4],
            baseColorTexture=self.Image.new('RGBA', (4, 4), (80, 120, 160, 40)),
            alphaMode=mode, alphaCutoff=cutoff)
        self.simulation._write_material(stage, '/World/Looks/test', material, directory)
        shader = self.UsdShade.Shader(stage.GetPrimAtPath('/World/Looks/test/surface'))
        return stage, shader

    def test_opaque_does_not_connect_texture_alpha_or_apply_source_factor_alpha(self):
        stage, shader = self.author('OPAQUE')
        self.assertEqual(shader.GetInput('opacity').Get(), 1)
        self.assertIsNone(shader.GetInput('opacity').GetConnectedSource())

    def test_mask_cutoff_zero_is_preserved_without_blending(self):
        stage, shader = self.author('MASK', 0)
        self.assertEqual(shader.GetInput('opacityThreshold').Get(), 0)
        self.assertEqual(shader.GetInput('opacity').Get(), 1)
        self.assertIsNone(shader.GetInput('opacity').GetConnectedSource())
        self.assertEqual(shader.GetPrim().GetCustomDataByKey('sceneagent:alphaMode'), 'MASK')

    def test_positive_mask_cutoff_retains_real_alpha_connection(self):
        stage, shader = self.author('MASK', .4)
        self.assertAlmostEqual(shader.GetInput('opacityThreshold').Get(), .4)
        self.assertIsNotNone(shader.GetInput('opacity').GetConnectedSource())

    def test_regular_material_recovery_uses_preview_inputs(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        stage = self.Usd.Stage.CreateNew(str(directory/'scene.usda'))
        source = self.trimesh.visual.material.PBRMaterial(baseColorFactor=[.2, .3, .4, 1.],
            metallicFactor=0, roughnessFactor=.7, alphaMode='OPAQUE')
        material = self.simulation._write_material(stage, '/World/Looks/test', source, directory)
        target, document = {'pbrMetallicRoughness': {}}, {}
        recovered = self.simulation._recover_material(stage, material, target, document, b'')
        self.assertNotIn('nativeMdlSha256', recovered)
        self.assertEqual(target['pbrMetallicRoughness']['baseColorFactor'], recovered['baseColor'])
        self.assertAlmostEqual(target['pbrMetallicRoughness']['metallicFactor'], 0)

    def test_auto_srgb_metadata_survives_texture_reencoding_without_factor_baking(self):
        from io import BytesIO
        from PIL import ImageCms, PngImagePlugin
        from pxr import Sdf
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        exif = self.Image.Exif()
        exif[0x8769] = {0xA001: 1}
        srgb = PngImagePlugin.PngInfo()
        srgb.add(b'sRGB', b'\x00')
        notes = PngImagePlugin.PngInfo()
        notes.add_text('Software', 'Texture authoring application')
        for format_, options in [('JPEG', {'exif': exif}),
                ('JPEG', {'exif': exif, 'icc_profile': ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()}),
                ('PNG', {'pnginfo': srgb}), ('PNG', {'pnginfo': notes})]:
            with self.subTest(format=format_, metadata=list(options)):
                source = directory / 'texture.png'
                self.Image.new('RGB', (4, 4), (64, 128, 230)).save(source, format=format_, **options)
                with self.Image.open(source) as image:
                    encoded = BytesIO()
                    image.save(encoded, format='PNG')
                data = encoded.getvalue()
                stage = self.Usd.Stage.CreateInMemory()
                material = self.UsdShade.Material.Define(stage, '/World/Material')
                shader = self.UsdShade.Shader.Define(stage, '/World/Material/Surface')
                shader.CreateIdAttr('UsdPreviewSurface')
                material.CreateSurfaceOutput().ConnectToSource(shader.CreateOutput('surface', Sdf.ValueTypeNames.Token))
                texture = self.UsdShade.Shader.Define(stage, '/World/Material/Texture')
                texture.CreateIdAttr('UsdUVTexture')
                texture.CreateInput('file', Sdf.ValueTypeNames.Asset).Set(str(source))
                texture.CreateInput('scale', Sdf.ValueTypeNames.Float4).Set((.25, .5, .75, 1))
                shader.CreateInput('diffuseColor', Sdf.ValueTypeNames.Color3f).ConnectToSource(
                    texture.CreateOutput('rgb', Sdf.ValueTypeNames.Float3))
                target = {'pbrMetallicRoughness': {'baseColorTexture': {'index': 0}}}
                document = {'textures': [{'source': 0}], 'images': [{'bufferView': 0}],
                    'bufferViews': [{'byteOffset': 0, 'byteLength': len(data)}]}
                frozen = stage.GetRootLayer().ExportToString()
                self.simulation._recover_material(stage, material, target, document, data)
                self.assertEqual(target['pbrMetallicRoughness']['baseColorFactor'], [.25, .5, .75, 1])
                self.assertEqual(stage.GetRootLayer().ExportToString(), frozen)
                changed = BytesIO()
                self.Image.new('RGB', (4, 4), (1, 2, 3)).save(changed, format='PNG')
                document['bufferViews'][0]['byteLength'] = len(changed.getvalue())
                with self.assertRaisesRegex(ValueError, 'changed/baked'):
                    self.simulation._recover_material(stage, material, target, document, changed.getvalue())

    def normal_fixture(self, scale, bias, color_space='raw'):
        from pxr import Sdf
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'normal.png'
        self.Image.new('RGB', (4, 4), (80, 160, 240)).save(path)
        pixels = path.read_bytes()
        data = bytearray(b'geometry-prefix!'+pixels)
        stage = self.Usd.Stage.CreateInMemory()
        material = self.UsdShade.Material.Define(stage, '/World/Material')
        shader = self.UsdShade.Shader.Define(stage, '/World/Material/Surface')
        shader.CreateIdAttr('UsdPreviewSurface')
        material.CreateSurfaceOutput().ConnectToSource(shader.CreateOutput('surface', Sdf.ValueTypeNames.Token))
        texture = self.UsdShade.Shader.Define(stage, '/World/Material/Texture')
        texture.CreateIdAttr('UsdUVTexture')
        texture.CreateInput('file', Sdf.ValueTypeNames.Asset).Set(str(path))
        if scale is not None:
            texture.CreateInput('scale', Sdf.ValueTypeNames.Float4).Set(scale)
        if bias is not None:
            texture.CreateInput('bias', Sdf.ValueTypeNames.Float4).Set(bias)
        if color_space is not None:
            texture.CreateInput('sourceColorSpace', Sdf.ValueTypeNames.Token).Set(color_space)
        shader.CreateInput('normal', Sdf.ValueTypeNames.Normal3f).ConnectToSource(
            texture.CreateOutput('rgb', Sdf.ValueTypeNames.Float3))
        target = {'normalTexture': {'index': 0}}
        document = {'textures': [{'source': 0}], 'images': [{'bufferView': 0}],
            'bufferViews': [{'byteOffset': 16, 'byteLength': len(pixels)}],
            'buffers': [{'byteLength': len(data)}]}
        return stage, material, target, document, data

    def test_normal_recovery_ignores_unused_alpha_and_preserves_xy_strength(self):
        for scale, bias, strength in [((2, 2, 2, 2), (-1, -1, -1, -1), 1),
                ((.8, .8, 2, 9), (-.4, -.4, -1, 3), .4),
                ((-1, -1, 2, 1), (.5, .5, -1, 0), -.5),
                ((2.0000001, 2, 2, 1), (-1, -1, -1, 0), 1)]:
            with self.subTest(scale=scale, bias=bias):
                stage, material, target, document, data = self.normal_fixture(scale, bias)
                frozen = stage.GetRootLayer().ExportToString()
                self.simulation._recover_material(stage, material, target, document, data)
                self.assertAlmostEqual(target['normalTexture'].get('scale', 1), strength)
                self.assertEqual(stage.GetRootLayer().ExportToString(), frozen)

    def test_normal_recovery_derives_signed_directions_and_reuses_the_same_image(self):
        import hashlib
        from io import BytesIO
        import numpy as np
        for scale, bias, color_space in [
                (None, None, None),
                ((2, -2, 2, 1), (-1, 1, -1, 0), 'raw'),
                ((1, 2, 2, 1), (-.5, -1, -1, 0), 'raw'),
                ((2, 2, 1, 1), (-1, -1, -.5, 0), 'raw'),
                ((2, 2, 2, 1), (0, 0, -1, 0), 'raw')]:
            with self.subTest(scale=scale, bias=bias, color_space=color_space):
                stage, material, target, document, data = self.normal_fixture(scale, bias, color_space)
                original = bytes(data)
                frozen = stage.GetRootLayer().ExportToString()
                result = self.simulation._recover_material(stage, material, target, document, data)
                self.assertEqual(target['normalTexture'], {'index': 1, 'scale': 1})
                self.assertEqual(bytes(data[:len(original)]), original)
                view = document['bufferViews'][document['images'][1]['bufferView']]
                encoded = bytes(data[view['byteOffset']:view['byteOffset']+view['byteLength']])
                with self.Image.open(BytesIO(encoded)) as image:
                    actual = np.asarray(image, dtype=float)[0, 0]/255*2-1
                expected = np.array([80, 160, 240])/255
                if color_space is None:
                    expected = np.where(expected <= .04045, expected/12.92, ((expected+.055)/1.055)**2.4)
                expected = expected*np.array(scale[:3] if scale else [1, 1, 1])+np.array(bias[:3] if bias else [0, 0, 0])
                angle = np.degrees(np.arccos(np.clip(np.dot(expected, actual)/np.linalg.norm(expected)/np.linalg.norm(actual), -1, 1)))
                self.assertLess(angle, 1)
                provenance = result['normalTextureDerivation']
                self.assertEqual(provenance['sourceImageSha256'], hashlib.sha256(original[16:]).hexdigest())
                self.assertEqual(provenance['derivedImageSha256'], hashlib.sha256(encoded).hexdigest())
                self.assertLess(provenance['maxDirectionErrorDegrees'], 1)
                once = bytes(data)
                repeated = self.simulation._recover_material(stage, material, target, document, data)
                self.assertEqual(bytes(data), once)
                self.assertEqual(repeated, result)
                self.assertEqual(len(document['images']), 2)
                self.assertEqual(stage.GetRootLayer().ExportToString(), frozen)

    def test_normal_recovery_rejects_invalid_vectors_and_exported_strength(self):
        for scale, bias in [((0, 0, 0, 1), (0, 0, 0, 0)),
                ((float('nan'), 2, 2, 1), (-1, -1, -1, 0)),
                ((0, 0, 0, 1), (.001, 0, .1, 0))]:
            with self.subTest(scale=scale), self.assertRaisesRegex(ValueError, 'normal'):
                self.simulation._recover_material(*self.normal_fixture(scale, bias))
        fixture = self.normal_fixture((.8, .8, 2, 1), (-.4, -.4, -1, 0))
        fixture[2]['normalTexture']['scale'] = 2
        with self.assertRaisesRegex(ValueError, 'unexpected existing normal scale'):
            self.simulation._recover_material(*fixture)

    def test_derived_normal_keeps_original_pixel_and_derived_hash_guards(self):
        from pxr import UsdShade
        stage, material, target, document, data = self.normal_fixture(None, None, None)
        texture = UsdShade.Shader(stage.GetPrimAtPath('/World/Material/Texture'))
        path = Path(texture.GetInput('file').Get().path)
        original = path.read_bytes()
        self.Image.new('RGB', (4, 4), (1, 2, 3)).save(path)
        with self.assertRaisesRegex(ValueError, 'changed/baked'):
            self.simulation._recover_material(stage, material, target, document, data)
        path.write_bytes(original)
        self.simulation._recover_material(stage, material, target, document, data)
        view = document['bufferViews'][document['images'][1]['bufferView']]
        data[view['byteOffset']] ^= 1
        with self.assertRaisesRegex(ValueError, 'existing derived normal image differs'):
            self.simulation._recover_material(stage, material, target, document, data)

    def test_absent_native_diffuse_input_uses_locked_mdl_default(self):
        from pxr import Sdf
        import os
        mdl = Path(os.environ.get('EMBODIEDSTUDIO_OMNIPBR_FIXTURE', 'OmniPBR.mdl'))
        if not mdl.is_file():
            self.skipTest('locked native MDL fixture unavailable')
        stage = self.Usd.Stage.CreateInMemory()
        material = self.UsdShade.Material.Define(stage, '/World/Looks/default')
        shader = self.UsdShade.Shader.Define(stage, '/World/Looks/default/shader')
        prim = shader.GetPrim()
        prim.CreateAttribute('info:implementationSource', Sdf.ValueTypeNames.Token).Set('sourceAsset')
        prim.CreateAttribute('info:mdl:sourceAsset', Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(str(mdl)))
        prim.CreateAttribute('info:mdl:sourceAsset:subIdentifier', Sdf.ValueTypeNames.Token).Set('OmniPBR')
        shader.CreateOutput('out', Sdf.ValueTypeNames.Token)
        material.CreateSurfaceOutput('mdl').ConnectToSource(shader.ConnectableAPI(), 'out')
        self.assertFalse(shader.GetInput('diffuse_color_constant'))
        target = {'pbrMetallicRoughness': {'metallicFactor': 0}}
        recovered = self.simulation._recover_material(stage, material, target, {}, b'')
        self.assertEqual(recovered['baseColor'], [.2, .2, .2, 1])
        self.assertEqual(target['pbrMetallicRoughness']['baseColorFactor'], [.2, .2, .2, 1])
        prim.GetAttribute('info:mdl:sourceAsset:subIdentifier').Set('unknown')
        with self.assertRaisesRegex(ValueError, 'unsupported native material shader'):
            self.simulation._recover_material(stage, material, target, {}, b'')


if __name__ == '__main__':
    unittest.main()
