import copy
import math
import unittest

from runtime.scene import World, Task


class SceneTreeTests(unittest.TestCase):
    def test_metre_texture_period_is_frozen_without_an_implicit_repeat(self):
        world = World()
        world.box('thin_worktop', (1.2, .7, .025), material='/prepared/report.json', texture_period_m=.3)
        world.cylinder('leg', .025, .7, material='/prepared/report.json', texture_period_m=.3)
        for node in world.nodes.values():
            self.assertEqual(node['texture_period_m'], .3)
            self.assertIsNone(node['uv_repeat'])
        self.assertEqual(World.restore(world.snapshot()).snapshot(), world.snapshot())

    def test_texture_period_rejects_invalid_values_and_explicit_repeat_conflicts(self):
        for fields in ({'texture_period_m': 0}, {'texture_period_m': -.3},
                       {'texture_period_m': float('nan')}, {'texture_period_m': float('inf')},
                       {'texture_period_m': .3, 'uv_repeat': (1, 1)}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, 'texture_period_m|uv_repeat'):
                World().box('box', (1, 1, .02), **fields)
        world = World()
        world.box('box', (1, 1, .02))
        snapshot = world.snapshot()
        snapshot['nodes'][0].update(texture_period_m=.3, uv_repeat=[1, 1])
        with self.assertRaisesRegex(ValueError, 'texture_period_m|uv_repeat'):
            World.restore(snapshot)

    def test_ownership_does_not_move_coordinates_or_create_physical_attachment(self):
        world = World()
        world.space('room', at=(8, 0, 0), bounds={'min': [-2, -2, 0], 'max': [2, 2, 3]},
                    purpose='robot workshop', requirements=['request:room'], completion=['layout fits'])
        world.space('table_frame', at=(.5, 0, .7), owner='room')
        world.box('cube', (.05, .05, .05), at=(.1, 0, .1), owner='room', frame='table_frame')
        world.box('table', (1, 1, .1), at=(.5, 0, .7), owner='room')
        world.relate('supported_by', 'cube', 'table')
        self.assertEqual(world.position('cube'), [.6, 0, .7999999999999999])
        node = world.snapshot()['nodes'][2]
        self.assertEqual(node['owner'], 'room')
        self.assertEqual(node['frame'], 'table_frame')
        self.assertNotIn('parent', node)
        self.assertNotIn('yaw', node)
        self.assertEqual(world.snapshot()['relations'][0]['kind'], 'supported_by')
        self.assertIsNone(node['joint'])

    def test_transform_composes_full_quaternions_and_legacy_parent_yaw(self):
        world = World()
        world.space('root', at=(1, 2, 3), orientation=(math.sqrt(.5), math.sqrt(.5), 0, 0))
        world.space('child', at=(0, 1, 0), frame='root')
        pos, quat = world.transform('child')
        for actual, expected in zip(pos, [1, 2, 4]):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(quat[1], math.sqrt(.5))
        world.space('legacy', at=(1, 0, 0), parent='child', yaw=math.pi/2)
        self.assertEqual(world.nodes['legacy']['owner'], 'child')
        self.assertEqual(world.nodes['legacy']['frame'], 'child')
        self.assertAlmostEqual(world.position('legacy')[0], 2)
        snapshot = world.snapshot()
        self.assertEqual(snapshot['schema'], 'sceneagent-studio-world-v3')
        self.assertEqual(World.restore(snapshot).snapshot(), snapshot)
        snapshot['nodes'][0]['at'][0] = 100
        self.assertEqual(world.position('root')[0], 1)

    def test_deep_tree_validation_and_transform_do_not_use_python_recursion(self):
        world = World()
        for index in range(1500):
            world.space(f'n_{index}', at=(.001, 0, 0), parent=f'n_{index-1}' if index else None)
        self.assertAlmostEqual(world.position('n_1499')[0], 1.5)
        self.assertEqual(world.validate()['maxDepth'], 1500)
        self.assertEqual(len(world.context('n_1490')['subtree']), 10)
        self.assertEqual(len(World.restore(world.snapshot()).nodes), 1500)

    def test_context_contains_subtree_and_external_frame_and_relation_dependencies(self):
        world = World()
        world.space('building')
        world.space('room_a', owner='building')
        world.space('room_b', owner='building', at=(4, 0, 0))
        world.box('shelf', (1, 1, 1), owner='room_b')
        world.box('cube', (.1, .1, .1), owner='room_a', frame='room_b')
        world.relate('supported_by', 'cube', 'shelf', requirement='request:support')
        context = world.context('room_a')
        self.assertEqual({n['id'] for n in context['subtree']}, {'room_a', 'cube'})
        self.assertEqual([n['id'] for n in context['ancestors']], ['building'])
        self.assertEqual({n['id'] for n in context['externalNodes']}, {'room_b', 'shelf'})
        self.assertEqual(context['frameChains']['cube'], ['room_b'])
        self.assertEqual(context['relations'][0]['target'], 'shelf')
        self.assertEqual({n['id'] for n in world.context()['subtree']}, set(world.nodes))
        self.assertEqual(world.context()['ancestors'], [])

    def test_validation_rejects_broken_owner_frame_relation_and_support_cycles(self):
        world = World()
        world.space('a')
        world.space('b', owner='a', frame='a')
        world.box('cube', (.1, .1, .1), owner='b')
        for field in ('owner', 'frame'):
            bad = copy.deepcopy(world.snapshot())
            bad['nodes'][0][field] = 'b'
            with self.assertRaisesRegex(ValueError, 'cycle'):
                World.restore(bad)
            bad['nodes'][0][field] = 'missing'
            with self.assertRaisesRegex(ValueError, 'missing|unknown'):
                World.restore(bad)
        world.relate('supported_by', 'cube', 'b')
        world.relate('supported_by', 'b', 'a')
        world.relate('supported_by', 'a', 'cube')
        with self.assertRaisesRegex(ValueError, 'cycle'):
            world.validate()
        bad = copy.deepcopy(world.snapshot(validate=False))
        bad['relations'][0]['target'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'missing|unknown'):
            World.restore(bad)

    def test_validation_rejects_duplicate_snapshot_ids_nonfinite_and_bad_joint(self):
        world = World()
        world.box('cube', (.1, .1, .1))
        base = world.snapshot()
        bad = copy.deepcopy(base)
        bad['nodes'].append(copy.deepcopy(bad['nodes'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            World.restore(bad)
        for change in ({'at': [float('nan'), 0, 0]}, {'size': [0, .1, .1]},
                       {'orientation': [0, 0, 0, 0]},
                       {'joint': {'type': 'slide', 'axis': [0, 0, 0], 'range': [0, .1]}},
                       {'joint': {'type': 'slide', 'frame': 'missing', 'range': [0, .1]}}):
            bad = copy.deepcopy(base)
            bad['nodes'][0].update(change)
            with self.assertRaises(ValueError):
                World.restore(bad)

    def test_legacy_worlds_require_their_frozen_worker_instead_of_silent_upgrade(self):
        old = {'schema': 'sceneagent-studio-world-v1', 'units': 'meters', 'upAxis': 'Z', 'seed': 0,
               'robot': {'model': 'franka_emika_panda', 'at': [0, 0, .74]}, 'interactions': [],
               'nodes': [{'id': 'room', 'kind': 'space', 'parent': None, 'yaw': math.pi/2, 'at': [1, 2, 0]},
                         {'id': 'cube', 'kind': 'box', 'parent': 'room', 'yaw': 0, 'at': [1, 0, .8],
                          'size': [.1, .1, .1], 'color': [.7, .7, .7, 1], 'mass': 0, 'joint': None}]}
        v2 = {**World().snapshot(), 'schema': 'sceneagent-studio-world-v2', 'robot': old['robot']}
        for snapshot in (old, v2):
            with self.subTest(schema=snapshot['schema']), self.assertRaisesRegex(ValueError, 'frozen worker'):
                World.restore(snapshot)

    def test_explicit_legacy_world_entry_is_rejected_before_construction(self):
        with self.assertRaisesRegex(ValueError, 'frozen worker'):
            World(native=False)

    def test_diff_uses_actual_snapshots_and_preserves_stable_id(self):
        world = World()
        world.space('room')
        world.box('cube', (.1, .1, .1), owner='room')
        changed = World.restore(world.snapshot())
        changed.nodes['cube']['at'][0] = .2
        changed.box('goal', (.1, .1, .01), owner='room')
        changed.relate('contained_in', 'cube', 'room')
        diff = world.diff(changed)
        self.assertEqual([node['id'] for node in diff['added']], ['goal'])
        self.assertEqual(diff['removed'], [])
        self.assertEqual(diff['changed'][0]['id'], 'cube')
        self.assertEqual(diff['changed'][0]['fields'], ['at'])
        self.assertEqual(diff['relations']['added'][0]['kind'], 'contained_in')
        self.assertEqual(world.diff(world)['changed'], [])
        changed.nodes['room']['new_annotation'] = None
        self.assertEqual(world.diff(changed)['changed'][1]['fields'], ['new_annotation'])

    def test_task_binds_real_objects_and_frames_without_system_scoring(self):
        world = World()
        world.space('room', at=(2, 3, 0), yaw=math.pi/2)
        world.box('cube', (.04, .04, .04))
        rubric = {'sourceRef': 'rubrics.py', 'helperRefs': []}
        task = Task('motion', bindings={'subject': {'objectId': 'cube'}, 'reference': {'frame': 'room'}},
                    parameters={'goal': [.2, 0, .1]}, rubric=rubric)
        bound = task.bind(world)
        self.assertEqual(bound['bindings'], {'subject': {'objectId': 'cube'}, 'reference': {'frame': 'room'}})
        self.assertEqual(bound['parameters'], {'goal': [.2, 0, .1]})
        with self.assertRaises(ValueError):
            Task('bad', bindings={'reference': {'frame': 'missing'}}, rubric=rubric).bind(world)
        with self.assertRaises(ValueError):
            Task('bad', bindings={'subject': {'objectId': 'room'}}, rubric=rubric).bind(world)

    def test_interaction_declarations_do_not_imply_native_execution_and_reject_unknown_modes(self):
        world = World()
        world.box('cube', (.1, .1, .1))
        world.when('touch', 'cube', 'robot_contact', {'target': 'cube'}, mode='edge')
        self.assertEqual(world.interactions[0]['mode'], 'edge')
        with self.assertRaisesRegex(ValueError, 'native.*interactions'):
            world.snapshot()
        with self.assertRaises(ValueError):
            world.when('bad', 'cube', 'robot_contact', {'target': 'cube'}, mode='unknown')


def _v5_recording():
    return {'primaryCameraId': 'front', 'cameras': [{
        'cameraId': 'front', 'frame': 'world', 'at': [1., 0., 1.],
        'orientation': [1., 0., 0., 0.], 'resolution': [320, 240],
        'rateHz': 25, 'modalities': ['rgb'], 'video': True,
        'required': True, 'source': 'simulation_observer'}]}


def _v5_task(**fields):
    return Task('numeric_fixture', bindings={'subject': 'cube'},
                rubric={'sourceRef': 'rubrics.py', 'helperRefs': []}, **fields)


class RecordingTaskContracts(unittest.TestCase):
    def world(self):
        world = World(); world.box('cube', [.05, .05, .05])
        world.io = {'clock': {'physicsRateHz': 200, 'controlRateHz': 50}, 'ports': {}, 'streams': {}}
        return world

    def test_recording_and_nondefault_physical_budget_are_frozen(self):
        frozen = _v5_task(recording=_v5_recording(), execution={'physicalSeconds': 80}).bind(self.world())
        self.assertEqual(frozen['recording'], _v5_recording())
        self.assertEqual(frozen['execution'], {'physicalSeconds': 80})

    def test_long_finite_physical_duration_is_frozen_without_a_platform_ceiling(self):
        self.assertEqual(_v5_task().bind(self.world())['execution'], {'physicalSeconds': 30})
        for seconds in (301, 1800, 86400.):
            with self.subTest(seconds=seconds):
                frozen = _v5_task(execution={'physicalSeconds': seconds}).bind(self.world())
                self.assertEqual(frozen['execution'], {'physicalSeconds': seconds})
                self.assertEqual(Task(**frozen).bind(self.world()), frozen)

    def test_odd_rgb_resolution_is_preserved_without_video(self):
        for resolution in ([321, 240], [320, 241], [321, 241]):
            declaration = _v5_recording()
            declaration['cameras'][0].update(resolution=resolution, video=False)
            with self.subTest(resolution=resolution):
                frozen = _v5_task(recording=declaration).bind(self.world())
                self.assertEqual(frozen['recording']['cameras'][0]['resolution'], resolution)

    def test_odd_video_resolution_rejects_the_current_encoder_limit_early(self):
        for resolution in ([321, 240], [320, 241], [321, 241]):
            declaration = _v5_recording()
            declaration['cameras'][0]['resolution'] = resolution
            with self.subTest(resolution=resolution), self.assertRaisesRegex(ValueError, 'current H.264/yuv420p encoder'):
                _v5_task(recording=declaration).bind(self.world())

    def test_duplicate_camera_or_unknown_mount_cannot_be_frozen(self):
        for kind in ('duplicate', 'missing_mount'):
            declaration = _v5_recording()
            if kind == 'duplicate': declaration['cameras'].append(copy.deepcopy(declaration['cameras'][0]))
            else: declaration['cameras'][0]['frame'] = 'missing'
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                _v5_task(recording=declaration).bind(self.world())

    def test_false_frequency_and_budget_claims_are_rejected(self):
        for frequency in (True, 30, 60, 0):
            declaration = _v5_recording(); declaration['cameras'][0]['rateHz'] = frequency
            with self.subTest(frequency=frequency), self.assertRaises(ValueError):
                _v5_task(recording=declaration).bind(self.world())
        for seconds in (True, False, None, '301', 0, -1, float('nan'), float('inf'), -float('inf')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                _v5_task(execution={'physicalSeconds': seconds}).bind(self.world())

    def test_recording_uses_the_actual_clock_instead_of_legacy_frequencies(self):
        declaration = _v5_recording(); declaration['cameras'][0]['rateHz'] = 40
        self.assertEqual(_v5_task(recording=declaration).bind(self.world())['recording'], declaration)

    def test_recording_requires_explicit_native_mount_paths(self):
        declaration = _v5_recording()
        declaration['cameras'][0]['frame'] = {'robotLink': 'panda_hand'}
        from runtime.scene import validate_recording
        for world in (None, self.world()):
            with self.subTest(world=world is not None), self.assertRaisesRegex(ValueError, 'nativePath'):
                validate_recording(declaration, world)



if __name__ == '__main__':
    unittest.main()
