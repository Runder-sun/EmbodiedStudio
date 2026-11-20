"""Isolated CPU sensor-contract fixtures; not generated scene deliverables."""
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import numpy as np

from runtime.scene import World
from runtime.simulation import Environment, recording_camera_pose
from runtime.robot import native_control_cycles
from runtime.worker import controller_command


class SensorContractTests(unittest.TestCase):
    def test_nonstandard_resolution_intrinsics_match_actual_usd_float_lens(self):
        import sys
        from pxr import Gf, Usd, UsdGeom
        from runtime.simulation import _write_camera
        stage = Usd.Stage.CreateInMemory()
        class CameraFixture:
            def __init__(self, camera):
                self.camera = camera
            def __getattr__(self, name):
                return getattr(self.camera, name)
            def GetPrim(self):
                # Only the absent SDK schema registration is stubbed; lens attributes are real USD.
                return SimpleNamespace(ApplyAPI=lambda schema: None)
        geometry = SimpleNamespace(XformOp=UsdGeom.XformOp, Camera=SimpleNamespace(
            Define=lambda stage,path: CameraFixture(UsdGeom.Camera.Define(stage,path))))
        with patch.dict(sys.modules, {'pxr':SimpleNamespace(Gf=Gf, UsdGeom=geometry)}):
            spec = _write_camera(stage, {'width':97,'height':64,'worldFromCamera':np.eye(4).tolist()})
        native = UsdGeom.Camera(stage.GetPrimAtPath(spec['primPath']))
        actual = {'fx':97*native.GetFocalLengthAttr().Get()/native.GetHorizontalApertureAttr().Get(),
                  'fy':64*native.GetFocalLengthAttr().Get()/native.GetVerticalApertureAttr().Get(),
                  'cx':48.5,'cy':32.}
        self.assertEqual(spec['intrinsics'], actual)
        from runtime.simulation import camera_intrinsics
        self.assertEqual(camera_intrinsics(97,64), actual)
        self.assertAlmostEqual(native.GetClippingRangeAttr().Get()[0], .01, places=8)
        self.assertEqual(native.GetClippingRangeAttr().Get()[1], 1000.)

    def test_optional_camera_initialization_failure_is_local_and_releases_partial_sensor(self):
        env = Environment.__new__(Environment)
        env.camera_resources, env.camera_failures, env.latest_camera_frames = {}, {}, {}
        env.camera_specs = {'optional': {'required': False}}
        partial = Mock()
        class Sensor:
            def __new__(cls, *args, **kwargs):
                return partial
            def __init__(self, *args, **kwargs):
                raise RuntimeError('sensor construction failed after allocation')
        with self.assertRaisesRegex(RuntimeError, 'sensor construction'):
            env._initialize_camera('optional', {'primPath': '/camera'}, Mock(), Sensor, Mock(), Mock())
        env._disable_optional_camera('optional', RuntimeError('sensor construction failed'))
        partial._invalidate_sensor.assert_called_once()
        self.assertEqual(env.latest_camera_frames['optional']['status'], 'unavailable')

    def test_required_camera_initialization_failure_stays_fatal(self):
        env = Environment.__new__(Environment)
        env.camera_specs = {'required': {'required': True}}
        with self.assertRaisesRegex(RuntimeError, 'required camera'):
            env._disable_optional_camera('required', RuntimeError('required camera failed'))

    def test_optional_annotator_attach_failure_detaches_partial_attachment(self):
        env = Environment.__new__(Environment)
        env.stage = object()
        env.camera_resources, env.camera_failures, env.latest_camera_frames = {}, {}, {}
        spec = {'primPath': '/camera', 'worldFromCamera': np.eye(4).tolist(), 'required': False}
        env.camera_specs = {'optional': spec}
        sensor, reference, read_time = Mock(), Mock(), Mock()
        class Sensor:
            def __new__(cls):
                return sensor
            def __init__(self, *args, **kwargs):
                pass
        rep = SimpleNamespace(AnnotatorRegistry=SimpleNamespace(get_annotator=Mock(side_effect=[reference, read_time])))
        reference.attach.side_effect = RuntimeError('attach failed after attachment')
        with patch('runtime.simulation._usd_world', return_value=np.eye(4)):
            try:
                env._initialize_camera('optional', spec, Mock(), Sensor, rep, Mock())
            except RuntimeError as exc:
                env._disable_optional_camera('optional', exc)
        reference.detach.assert_called_once()
        sensor._invalidate_sensor.assert_called_once()
        self.assertEqual(env.latest_camera_frames['optional']['status'], 'unavailable')

    def test_wrist_pose_uses_current_actual_link_matrix(self):
        world = World(native=True)
        mount = np.eye(4)
        mount[:3, 3] = [.3, -.2, .8]
        camera = {'frame': {'nativePath':'/World/fixture_robot/tool'}, 'at': [.01, 0, .07], 'orientation': [1, 0, 0, 0]}
        stage=SimpleNamespace(GetPrimAtPath=lambda path:path=='/World/fixture_robot/tool')
        with patch('runtime.simulation._usd_world',side_effect=lambda stage,path:mount.copy()):
            self.assertTrue(np.allclose(recording_camera_pose(world,camera,stage=stage), [[1, 0, 0, .31], [0, 1, 0, -.2], [0, 0, 1, .87], [0, 0, 0, 1]]))
            mount[0,3] += .2
            self.assertAlmostEqual(recording_camera_pose(world,camera,stage=stage)[0,3],.51)
            with self.assertRaisesRegex(ValueError,'camera/construction frame has no native prim: /World/missing'):
                recording_camera_pose(world,{**camera,'frame':{'nativePath':'/World/missing'}},stage=stage)

    def test_sensor_mount_is_readonly_and_not_given_to_grader(self):
        with TemporaryDirectory() as temporary, patch('runtime.worker.shutil.which', return_value='/bin/bwrap'):
            command = controller_command(sensors=Path(temporary))
            position = command.index('/sensors')
            self.assertEqual(command[position-2:position+1], ['--ro-bind', str(Path(temporary).resolve()), '/sensors'])
            self.assertNotIn('/sensors', controller_command('--rubric'))

    def test_all_camera_resources_close_even_if_one_detach_fails(self):
        env = Environment.__new__(Environment)
        with TemporaryDirectory() as temporary:
            env.directory = Path(temporary)
            first, second, sensor = Mock(), Mock(), Mock()
            first.detach.side_effect = RuntimeError('actual detach failure')
            env.camera_resources = {'one': {'reference': first, 'referenceAttached': True, 'sensor': sensor},
                                    'two': {'reference': second, 'referenceAttached': True}}
            with self.assertRaisesRegex(RuntimeError, 'actual detach failure'):
                env._close_camera_resources()
            sensor.detach_annotators.assert_called_once_with(['rgb'])
            second.detach.assert_called_once()
            self.assertIsNone(env.camera_resources['one']['reference'])

    def test_frozen_longer_horizon_does_not_remove_control_alignment(self):
        clock={'physicsRateHz':500,'controlRateHz':50}
        self.assertEqual(native_control_cycles(.02,clock,31,60),1)
        with self.assertRaisesRegex(ValueError, 'budget'):
            native_control_cycles(.02,clock,60,60)
        with self.assertRaisesRegex(ValueError,'complete.*periods'):
            native_control_cycles(.021,clock,31,60)

    def test_sensor_pose_changes_are_not_physics_parameter_changes(self):
        try:
            from pxr import Usd, UsdGeom
        except ImportError as exc:
            self.skipTest(str(exc))
        stage = Usd.Stage.CreateInMemory()
        camera=UsdGeom.Camera.Define(stage,'/camera')
        camera_move=camera.AddTranslateOp();camera_move.Set((0,0,1))
        cube = UsdGeom.Cube.Define(stage, '/static_cube')
        cube_move = cube.AddTranslateOp()
        cube_move.Set((0, 0, 0))
        original=(cube_move.Get(),cube.GetSizeAttr().Get(),list(cube.GetPrim().GetAppliedSchemas()))
        camera_move.Set((.2, 0, 1))
        self.assertEqual(original,(cube_move.Get(),cube.GetSizeAttr().Get(),list(cube.GetPrim().GetAppliedSchemas())))
        cube_move.Set((.2, 0, 0))
        self.assertNotEqual(original[0],cube_move.Get())
        self.assertEqual(camera_move.Get(),(.2,0,1))


if __name__ == '__main__':
    unittest.main()
