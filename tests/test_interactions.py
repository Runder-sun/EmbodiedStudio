import unittest
from runtime.interactions import Interactions


class InteractionTests(unittest.TestCase):
    def test_contact_edges_use_current_signal_and_can_fire_again_after_release(self):
        rule = {'id': 'touch', 'target': 'button', 'event': 'robot_contact', 'mode': 'edge',
                'effect': {'target': 'lamp', 'state': {'on': True}}}
        kernel = Interactions([rule])
        self.assertEqual(len(kernel.update(0, {'button'}, {})), 1)
        self.assertEqual(len(kernel.update(.01, {'button'}, {})), 0)
        kernel.update(.02, set(), {})
        self.assertEqual(len(kernel.update(.03, {'button'}, {})), 1)
        kernel.reset()
        self.assertEqual(kernel.state, {})
        self.assertEqual(kernel.events, [])
        self.assertEqual(len(kernel.update(0, {'button'}, {})), 1)

    def test_once_and_continuous_modes_are_not_confused(self):
        for mode, expected in [('once', 1), ('continuous', 2)]:
            rule = {'id': 'press', 'target': 'button', 'event': 'joint_at_least', 'threshold': .01,
                    'mode': mode, 'effect': {'target': 'lamp', 'state': {'on': True}}}
            kernel = Interactions([rule])
            kernel.update(0, set(), {'button': .02})
            kernel.update(.01, set(), {'button': .02})
            self.assertEqual(len(kernel.events), expected)


if __name__ == '__main__':
    unittest.main()
