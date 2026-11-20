"""SYSTEM_VERIFICATION_ONLY: supplemental policy resources, never model/task quality."""
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
import sys

from examples import policy_resources


class PolicyResourcesTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root/'source'; source.mkdir()
        (source/'weights.bin').write_bytes(b'original verification weights')
        (source/'deps').mkdir(); (source/'deps/helper.py').write_text('VALUE = 7\n')
        manifest = policy_resources.freeze(source, root/'frozen')
        return root, source, manifest

    def test_freeze_copies_complete_resources_without_linking_mutable_source(self):
        root, source, manifest = self.fixture()
        original = policy_resources.verify(manifest)
        (source/'weights.bin').write_bytes(b'changed producer input')
        self.assertEqual(policy_resources.verify(manifest), original)
        self.assertEqual((manifest.parent/'files/weights.bin').read_bytes(), b'original verification weights')
        self.assertEqual(set(original['files']), {'weights.bin', 'deps/helper.py'})

    def test_modified_missing_or_added_resource_is_rejected(self):
        for change in ('modify', 'delete', 'add', 'symlink'):
            with self.subTest(change=change):
                root, source, manifest = self.fixture()
                target = manifest.parent/'files/weights.bin'; target.chmod(0o644)
                if change == 'modify': target.write_bytes(b'changed')
                elif change == 'delete': target.unlink()
                elif change == 'add': (manifest.parent/'files/unlisted.bin').write_bytes(b'private')
                else:
                    target.unlink(); target.symlink_to(source/'weights.bin')
                with self.assertRaises(ValueError): policy_resources.verify(manifest)

    def test_freeze_rejects_links_and_existing_destination(self):
        root, source, manifest = self.fixture()
        with self.assertRaises(FileExistsError): policy_resources.freeze(source, manifest.parent)
        (source/'alias').symlink_to(source/'weights.bin')
        with self.assertRaises(ValueError): policy_resources.freeze(source, root/'bad')
        self.assertFalse((root/'bad').exists())

    def context(self, root, manifest):
        worker = root/'runtime/frozen/runtime/worker.py'
        worker.parent.mkdir(parents=True); worker.write_text('# original frozen worker\n')
        request = dict(operation='evaluate', root=str(root), projectId='p_fixture', revision='rev',
                       taskId='task', candidate={'sourceDigests':{'candidate.py':'a'*64}})
        context = dict(worker=str(worker), workerSha256=hashlib.sha256(worker.read_bytes()).hexdigest(),
                       python='/python', resourceManifest=str(manifest),
                       resourceManifestSha256=hashlib.sha256(manifest.read_bytes()).hexdigest(), binding=request)
        args = ['--unshare-all', '--die-with-parent', '--new-session', '--clearenv',
                '--setenv', 'PATH', '/usr/bin:/bin', '/python', str(worker), '--controller']
        return context, request, args

    def test_candidate_has_separate_persistent_output_without_writable_inputs(self):
        root, source, manifest = self.fixture(); context, request, args = self.context(root, manifest)
        output = root/'policy-output'; output.mkdir()
        context['policyOutput'] = str(output)
        result = policy_resources.mount_arguments(args, context, request)
        index = result.index('--bind')
        self.assertEqual(result[index:index+3], ['--bind', str(output), '/policy-output'])
        self.assertEqual(result[index+3:index+6], ['--setenv', 'SCENEAGENT_POLICY_OUTPUT_ROOT', '/policy-output'])
        self.assertIn('--ro-bind', result)
        self.assertEqual(result[:len(args)-3], args[:-3])

    def test_only_verified_evaluation_controller_receives_readonly_mount(self):
        root, source, manifest = self.fixture(); context, request, args = self.context(root, manifest)
        result = policy_resources.mount_arguments(args, context, request)
        self.assertEqual(result[:len(args)-3], args[:-3])
        self.assertEqual(result[-3:], args[-3:])
        self.assertEqual(result[len(args)-3:-3], ['--ro-bind', str(manifest.parent/'files'), '/policy-resources',
                         '--setenv', 'SCENEAGENT_POLICY_RESOURCE_ROOT', '/policy-resources'])
        self.assertNotIn('--share-net', result)
        for entry in ('--rubric', '--native-build-source', '--mdl-audit'):
            other = args[:-1]+[entry]
            self.assertEqual(policy_resources.mount_arguments(other, context, None), other)

    def test_wrong_scope_missing_isolation_or_changed_frozen_worker_are_rejected(self):
        root, source, manifest = self.fixture(); context, request, args = self.context(root, manifest)
        for field, value in [('operation','rollout'), ('projectId','p_other'), ('revision','other'),
                             ('taskId','other'), ('candidate',{'sourceDigests':{}})]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                policy_resources.mount_arguments(args, context, {**request,field:value})
        for flag in ('--unshare-all', '--clearenv', '--die-with-parent'):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                policy_resources.mount_arguments([arg for arg in args if arg != flag], context, request)
        Path(context['worker']).write_text('# modified frozen worker')
        with self.assertRaises(ValueError): policy_resources.mount_arguments(args, context, request)

    def test_only_bound_controller_receives_the_declared_socket(self):
        root, _, manifest = self.fixture(); context, request, args = self.context(root, manifest)
        endpoint = root/'infer.sock'
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(endpoint))
            context['policySocket'] = policy_resources.socket_identity(endpoint)
            result = policy_resources.mount_arguments(args, context, request)
            self.assertIn(['--ro-bind', str(endpoint.resolve()), '/policy-inference.sock'],
                          [result[i:i+3] for i in range(len(result))])
            self.assertIn('SCENEAGENT_POLICY_SOCKET', result)
            self.assertNotIn('--share-net', result)
            other = args[:-1]+['--rubric']
            self.assertEqual(policy_resources.mount_arguments(other, context, None), other)
            endpoint.rename(root/'old.sock')
            with socket.socket(socket.AF_UNIX) as replacement:
                replacement.bind(str(endpoint))
                with self.assertRaisesRegex(ValueError, 'policy socket changed'):
                    policy_resources.mount_arguments(args, context, request)
        link = root/'alias.sock'; link.symlink_to(endpoint)
        for invalid in (root, link, root/'missing.sock'):
            with self.assertRaises(ValueError):
                policy_resources.socket_identity(invalid)

    def test_changed_resource_manifest_after_execution_is_rejected(self):
        root, source, resources = self.fixture()
        self.context(root, resources)
        manifest = root/'environment.json'
        manifest.write_text(json.dumps({'runtimeExecution':{'python':sys.executable,'prefix':sys.prefix},
            'runtimeSnapshotRef':'runtime/frozen','projectId':'p_fixture','revision':'rev'}))
        candidate = root/'candidate.py'; candidate.write_text('# isolated candidate')
        evaluator = root/'evaluate.py'; evaluator.write_text('# fixture evaluator')
        bubblewrap = root/'native-bwrap'; bubblewrap.write_bytes(b'\x7fELF')
        output = root/'evaluation'
        args = SimpleNamespace(resources=str(resources),manifest=str(manifest),root=str(root),
            candidate=str(candidate),helper=[],task='task',evaluator=str(evaluator),output=str(output),
            gpu_index=0,gpu_uuid='GPU-fixture',max_seconds=120,worker_timeout_seconds=900,robot_setup_ref=None)

        def execute(*unused, **kwargs):
            context = json.loads((output/'context.json').read_text())
            (output/'controller-mount.json').write_text(json.dumps(context))
            kwargs['stdout'].write(b'{}')
            resources.write_text(resources.read_text()+'\n')
            return SimpleNamespace(returncode=0)

        with mock.patch.object(policy_resources.shutil,'which',return_value=str(bubblewrap)), \
             mock.patch.object(policy_resources.subprocess,'run',side_effect=execute):
            with self.assertRaisesRegex(ValueError,'evaluation input changed'):
                policy_resources.evaluate(args)
        self.assertFalse(json.loads((output/'result.json').read_text())['passed'])

    def test_full_candidate_output_is_hashed_and_retained_on_success_and_failure(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                root, source, resources = self.fixture(); self.context(root, resources)
                manifest = root/'environment.json'
                manifest.write_text(json.dumps({'runtimeExecution':{'python':sys.executable,'prefix':sys.prefix},
                    'runtimeSnapshotRef':'runtime/frozen','projectId':'p_fixture','revision':'rev'}))
                candidate = root/'candidate.py'; candidate.write_text('# isolated candidate')
                evaluator = root/'evaluate.py'; evaluator.write_text('# fixture evaluator')
                bubblewrap = root/'native-bwrap'; bubblewrap.write_bytes(b'\x7fELF')
                output = root/'evaluation'; data = bytes(range(256))*256
                args = SimpleNamespace(resources=str(resources),manifest=str(manifest),root=str(root),
                    candidate=str(candidate),helper=[],task='task',evaluator=str(evaluator),output=str(output),
                    gpu_index=0,gpu_uuid='GPU-fixture',max_seconds=120,worker_timeout_seconds=900,robot_setup_ref=None)

                def execute(*unused, **kwargs):
                    context = json.loads((output/'context.json').read_text())
                    (Path(context['policyOutput'])/'predictions.bin').write_bytes(data)
                    (output/'controller-mount.json').write_text(json.dumps(context))
                    kwargs['stdout'].write(b'{"executionStatus":"completed"}')
                    return SimpleNamespace(returncode=returncode)

                with mock.patch.object(policy_resources.shutil,'which',return_value=str(bubblewrap)), \
                     mock.patch.object(policy_resources.subprocess,'run',side_effect=execute):
                    if returncode:
                        with self.assertRaisesRegex(RuntimeError,'frozen evaluation failed'): policy_resources.evaluate(args)
                    else: policy_resources.evaluate(args)
                result = json.loads((output/'result.json').read_text())
                self.assertEqual(result['passed'], not returncode)
                self.assertEqual((output/'policy-output/predictions.bin').read_bytes(), data)
                self.assertEqual(result['policyOutput']['files'], {'predictions.bin':{
                    'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}})
                self.assertIn('not native observations or task scores', result['policyOutput']['trust'])

    def test_output_snapshot_rejects_links_and_nonregular_files_and_allows_no_diagnostics(self):
        for kind in ('file_link', 'directory_link', 'fifo'):
            with self.subTest(kind=kind):
                root, source, _ = self.fixture(); output = root/'output'; output.mkdir()
                self.assertEqual(policy_resources._tree(output,allow_empty=True), {})
                target = output/'outside'
                if kind == 'file_link': target.symlink_to(source/'weights.bin')
                elif kind == 'directory_link': target.symlink_to(source,target_is_directory=True)
                else: os.mkfifo(target)
                with self.assertRaises(ValueError): policy_resources._tree(output,allow_empty=True)


if __name__ == '__main__': unittest.main()
