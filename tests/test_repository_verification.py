"""Evidence gates, using injected runtime observations (not live Docker)."""
import hashlib
import time
import unittest
from unittest.mock import patch

from src import repository_verification as verify
from src import repository_review as review


def observation(failed=False, name="test_boundary"):
    output = (f"{name} (test_app.Case.{name}) ... {'FAIL' if failed else 'ok'}\n"
              f"\nRan 1 test in 0.001s\n\n{'FAILED (failures=1)' if failed else 'OK'}")
    return {"complete": True, "execution_status": "completed", "exit_code": int(failed), "output_tail": output}


class RepositoryVerificationTests(unittest.TestCase):
    def setUp(self):
        self.files = {"app.py": "value = 1\n", "test_app.py": "# existing repository tests\n"}
        self.finding = {"id": "finding", "file": "app.py", "line": 1, "beforeCode": "value = 1",
                        "afterCode": "value = 2", "sourceSha256": hashlib.sha256(self.files['app.py'].encode()).hexdigest()}

    def check(self, runs, **kwargs):
        with patch.object(verify.runtime, '_checked', return_value='sha256:' + 'a' * 64), \
             patch.object(verify.runtime, '_inspect_image', side_effect=lambda image, _: image), \
             patch.object(verify.runtime, 'run_tests', side_effect=runs):
            return verify.check_patch(self.files, self.finding, 'python', ['test_app.py'], 'b' * 40,
                                      deadline=time.monotonic() + 30, **kwargs)

    def test_only_application_changes_and_both_runs_bind_identical_tests_and_image(self):
        snapshots = []
        def runner(root, image, config, deadline):
            snapshots.append((root.joinpath('app.py').read_text(), root.joinpath('test_app.py').read_text(), image, config))
            return observation(failed=len(snapshots) == 1)
        result = self.check(runner)
        self.assertEqual(result['status'], 'test_failure_reproduced')
        self.assertEqual(result['patchStatus'], 'passes_selected_tests')
        self.assertNotEqual(snapshots[0][0], snapshots[1][0])
        self.assertEqual(snapshots[0][1:], snapshots[1][1:])
        self.assertNotEqual(result['snapshotSha256'], result['candidateSha256'])
        self.assertEqual(result['testsSha256'], verify.digest({'test_app.py': self.files['test_app.py']}))

    def test_zero_tests_errors_skips_timeouts_cannot_reproduce(self):
        for output in ('Ran 0 tests in 0s\nOK', observation()['output_tail'].replace('... ok', '... ERROR'),
                       observation()['output_tail'].replace('... ok', "... skipped 'reason'"), ''):
            invalid = dict(observation(), output_tail=output)
            with self.subTest(output=output):
                result = self.check([invalid])
                self.assertEqual(result['status'], 'inconclusive')
                self.assertEqual(result['patchStatus'], 'not_verified')
        result = self.check([dict(observation(), complete=False)])
        self.assertEqual(result['status'], 'inconclusive')

    def test_passing_original_is_not_reproduced(self):
        self.assertEqual(self.check([observation()])['status'], 'not_reproduced')

    def test_failed_candidate_changed_test_ids_and_setup_error_preserve_original_failure(self):
        for second in (observation(True), observation(name='different_test'), dict(observation(), complete=False)):
            with self.subTest(second=second):
                result = self.check([observation(True), second])
                self.assertEqual(result['status'], 'test_failure_reproduced')
                self.assertEqual(result['patchStatus'], 'not_verified')

    def test_manifest_test_path_traversal_and_stale_source_cannot_execute(self):
        for path in ('test_app.py', 'requirements.txt', '../app.py', 'app.py'):
            files = dict(self.files, **{path: 'value = 1\n'})
            finding = dict(self.finding, file=path, sourceSha256='wrong' if path == 'app.py' else self.finding['sourceSha256'])
            with self.subTest(path=path), patch.object(verify.runtime, '_checked', side_effect=AssertionError('must not execute')):
                result = verify.check_patch(files, finding, 'python', ['test_app.py'], 'b' * 40, deadline=time.monotonic()+30)
                self.assertEqual(result['status'], 'inconclusive')

    def test_missing_docker_and_cancellation_are_not_passes(self):
        with patch.object(verify.runtime, '_checked', side_effect=verify.runtime.RuntimeFailure('missing')):
            result = verify.check_patch(self.files, self.finding, 'python', ['test_app.py'], 'b'*40, deadline=time.monotonic()+30)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(self.check([], cancelled=lambda: True)['patchStatus'], 'not_verified')

    def test_node_requires_assertions_and_matching_nonzero_totals(self):
        log = "not ok 1 - boundary\n  code: 'ERR_ASSERTION'\n# tests 1\n# pass 0\n# fail 1\n# cancelled 0\n# skipped 0\n# todo 0\n"
        measured = dict(observation(True), output_tail=log)
        self.assertEqual(verify._counts(measured, 'node')['count'], 1)
        for bad in (log.replace('ERR_ASSERTION', 'MODULE_NOT_FOUND'), log.replace('# tests 1', '# tests 0'), log.replace('# skipped 0', '# skipped 1')):
            self.assertIsNone(verify._counts(dict(measured, output_tail=bad), 'node'))

    def test_agent_uses_execution_observation_and_cannot_attach_it_to_a_different_patch(self):
        finding = {k: v for k,v in self.finding.items() if k not in {'id', 'sourceSha256'}}
        finding.update(title='Boundary', explanation='Existing assertion fails.', confidence='high', reproduction='Run test_app.')
        def action(tool, **args):
            return {'action': {'tool': tool, 'arguments': args, 'reason': 'Inspect evidence'}, 'provenance': {}}
        for changed in (False, True, 'omitted'):
            final = dict(finding, afterCode='value = 3') if changed else finding
            actions = iter([action('read_file', path='app.py'), action('read_file', path='test_app.py'),
                action('check_patch', finding=finding, runtime='python', tests=['test_app.py']),
                action('finish', summary='Reviewed.', findings=[] if changed == 'omitted' else [final])])
            def measured(files, row, *args, **kwargs):
                return {'findingId': row['id'], 'patchSha256': verify.digest([row['file'], row['beforeCode'], row['afterCode']]),
                        'status': 'test_failure_reproduced', 'patchStatus': 'passes_selected_tests'}
            with patch.object(review, 'check_patch', side_effect=measured):
                findings, metadata = review.review_repository(self.files, 'owner/repo', 'b'*40,
                    deadline=time.monotonic()+120, model=lambda *_: next(actions))
            self.assertEqual(metadata['status'], 'completed')
            self.assertEqual(len(metadata['testChecks']), 1)
            self.assertEqual(findings[0]['verification'], 'not_run' if changed is True else 'passes_selected_tests')
