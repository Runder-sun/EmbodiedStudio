"""SYSTEM_VERIFY_ONLY: named native control contracts, without an SDK double claiming physics."""
import unittest

from runtime import robot


class NativeControlTests(unittest.TestCase):
    def io(self):
        return {'schema': 'sceneagent-native-io-v1',
                'clock': {'physicsRateHz': 200, 'controlRateHz': 50},
                'ports': {
                    'arm': {'source': {'primitive': 'joint_position', 'path': '/World/arm', 'joints': ['j1', 'j2']},
                            'dtype': 'float32', 'shape': [2], 'units': ['rad', 'm'],
                            'limits': [[-1, 1], [0, .2]], 'persistence': 'persistent'},
                    'motor': {'source': {'primitive': 'joint_effort', 'path': '/World/base', 'joints': ['wheel']},
                              'dtype': 'float32', 'shape': [1], 'units': ['N*m'],
                              'limits': [[-4, 4]], 'persistence': 'per_physics_step'}},
                'streams': {}}

    def test_clock_requires_real_integer_divisor_without_a_fixed_robot_rate(self):
        self.assertEqual(robot.native_clock(self.io()['clock']), (200, 50, 4))
        for clock in ({'physicsRateHz': True, 'controlRateHz': 50},
                      {'physicsRateHz': 201, 'controlRateHz': 50},
                      {'physicsRateHz': 200, 'controlRateHz': 0},
                      {'physicsRateHz': 200, 'controlRateHz': 50, 'dt': .002}):
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                robot.native_clock(clock)

    def test_omission_retains_only_a_verified_persistent_native_target(self):
        requested = {'motor': [2]}
        self.assertEqual(robot.native_control_action(requested, self.io(), {'arm': [0, .1]}), requested)
        for active in ({}, {'arm': None}, {'arm': [float('nan'), .1]}):
            with self.subTest(active=active), self.assertRaises(ValueError):
                robot.native_control_action(requested, self.io(), active)
        with self.assertRaisesRegex(ValueError, 'explicit'):
            robot.native_control_action({'arm': [0, .1]}, self.io(), {'arm': [0, .1], 'motor': [2]})

    def test_whole_command_is_validated_before_any_sdk_write_and_no_coercion(self):
        for bad in ([True, .1], ['0', .1], [0], [[0, .1]], [0, float('inf')], [2, .1]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                robot.native_control_action({'arm': bad, 'motor': [0]}, self.io(), {})
        with self.assertRaises(ValueError):
            robot.native_control_action({'unknown': [0], 'motor': [0]}, self.io(), {'arm': [0, .1]})

    def test_physical_actuator_aliases_cannot_hide_overlapping_or_mixed_modes(self):
        io = self.io()
        robot.validate_native_ports(io['ports'])
        io['ports']['alias'] = {**io['ports']['arm'], 'source': {
            'primitive': 'joint_velocity', 'path': '/World/arm', 'joints': ['j2']},
            'shape': [1]}
        with self.assertRaisesRegex(ValueError, 'overlap'):
            robot.validate_native_ports(io['ports'])

    def test_unbounded_reference_still_must_be_finite_in_actual_native_dtype(self):
        io=self.io();io['ports']['arm']['limits']=[[None,None],[None,None]]
        self.assertEqual(robot.native_control_action({'arm':[2.,.1],'motor':[0]},io,{}),{'arm':[2.,.1],'motor':[0]})
        with self.assertRaises(ValueError):
            robot.native_control_action({'arm':[1e100,.1],'motor':[0]},io,{})

    def test_duration_uses_the_frozen_clock_and_rejects_partial_budget_cycles(self):
        self.assertEqual(robot.native_control_cycles(.04, self.io()['clock'], .02, .06), 2)
        for seconds, elapsed, limit in ((.021, 0, 1), (.04, .04, .06), (True, 0, 1), (0, 0, 1)):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                robot.native_control_cycles(seconds, self.io()['clock'], elapsed, limit)

    def test_low_frequency_periods_keep_whole_cycles_and_remaining_physics_budget(self):
        for rate in (1,5,10,50):
            clock={'physicsRateHz':100,'controlRateHz':rate};period=1/rate
            with self.subTest(rate=rate):
                self.assertEqual(robot.native_control_cycles(period,clock,0,period),1)
                for duration,elapsed,limit in ((period/2,0,period),(period,period,period),(True,0,2),('0.02',0,2)):
                    with self.assertRaises(ValueError):robot.native_control_cycles(duration,clock,elapsed,limit)


if __name__ == '__main__':
    unittest.main()
