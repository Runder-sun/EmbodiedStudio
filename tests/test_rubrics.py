"""SYSTEM_VERIFICATION_ONLY: two generated meanings share one bounded executor."""
import unittest
from unittest.mock import patch
import sys
from runtime import worker
from tests.test_generated_rubrics import task, selected


class GeneratedMeaningsTests(unittest.TestCase):
    def test_host_does_not_replace_generated_success_reward_or_termination(self):
        command = [sys.executable, '-c', 'from runtime.worker import rubric_entry; rubric_entry()']
        for success, terminated, reward in ((False, True, -3.), (True, False, 7.)):
            source = ("def evaluate(task, facts, state):\n    return {'state': None, 'result': "
                      + repr({'success': success, 'terminated': terminated, 'reward': reward, 'details': {'generated': True}})+'}\n')
            with self.subTest(success=success), patch.object(worker, 'controller_command', return_value=command):
                session = worker.RubricSession(task(), selected(source))
                try:
                    staged = session.preview('reset', {})
                    session.accept(staged)
                    self.assertEqual(session.last_result, {'success': success, 'terminated': terminated,
                        'reward': reward, 'details': {'generated': True}})
                finally:
                    session.close()


if __name__ == '__main__':
    unittest.main()
