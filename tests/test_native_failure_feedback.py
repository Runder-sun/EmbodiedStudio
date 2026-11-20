"""SYSTEM_VERIFICATION_ONLY: failed native child output reaches public job error."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from runtime import worker, jobs

class NativeFailureFeedbackTests(unittest.TestCase):
    def test_real_child_error_survives_worker_and_job_error_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def failed_child(*args,**kwargs):
                kwargs['stdout'].write(('startup noise\n'*1000+"Traceback (most recent call last):\nKeyError: 'fixture_resource'\n").encode())
                return SimpleNamespace(returncode=1)
            with patch.dict('sys.modules',{'runtime.simulation':SimpleNamespace(compile_native_world=None,export_preview=None)}), patch.object(worker,'native_build_command',return_value=['fixture']), patch.object(worker.subprocess,'run',side_effect=failed_child):
                with self.assertRaises(RuntimeError) as exc:
                    worker.build({'operation':'build','projectId':'fixture_failure','sourceFiles':{'scene.py':'def build(world): pass\n','tasks.py':'def build(world): return []\n'}},root)
            message=str(exc.exception)
            self.assertIn("KeyError: 'fixture_resource'",message)
            self.assertLess(len(message),2000)
            self.assertIn('native-build.log',message)
            work=root/'jobs/fixture';work.mkdir(parents=True)
            (work/'worker-failure.json').write_text(json.dumps({'error':'RuntimeError: '+message}))
            with self.assertRaisesRegex(RuntimeError,"fixture_resource"):
                jobs.verify_candidate(work,{},7,None)
            logs=list(root.glob('projects/fixture_failure/revisions/building-*/native-build.log'))
            self.assertEqual(len(logs),1)
            self.assertGreater(logs[0].stat().st_size,10000)
    def test_missing_log_keeps_original_failure(self):
        self.assertIn('native failure',worker.native_failure('native failure',Path('/missing/system-fixture.log')))

if __name__=='__main__':unittest.main()
