from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.config import Settings
from src.github_fetch import ChangedFile, Commit
from src.judge import Judgment, JudgeError
from src.main import execute, main
from src.memory import MemoryError
from src.report import render_report, terminal_text
from src.sandbox import SandboxResult


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings('example/repo', '', 'test-key', Path('/tmp/memory'),
                                 'test-image', 'pytest', 60, 35)
        self.commit = Commit('b' * 40, 'Ada', '2026-09-21', 'Add parser handling',
                             [ChangedFile('src/a.py', 'modified', 4, 2)], 4, 2, ['a' * 40])
        self.manifest = {'head': 'a' * 40, 'shas': [f'{i:040x}' for i in range(35)], 'dataset': 'test'}
        self.judgment = Judgment(flag='no', reason='Relevant tests accompany the change.', criteria_hit=[])
        self.tests = SandboxResult('pass', 0, '87 passed', 1.1)

    def run_flow(self, *, result=None, judgment=None, ancestor=True, model_error=None):
        client = Mock()
        client.commit.return_value = self.commit
        client.is_ancestor.return_value = ancestor
        client.diff.return_value = 'full diff'
        client.tree_paths.return_value = ['src/a.py', 'tests/test_a.py']
        memory = Mock()
        memory.manifest.return_value = self.manifest
        memory.search = AsyncMock(return_value='real retrieved context')
        with patch('src.main.GitHubClient', return_value=client), \
             patch('src.main.Memory', return_value=memory), patch('src.main.preflight'), \
             patch('src.main.judge_commit', return_value=judgment or self.judgment,
                   side_effect=model_error) as judge, \
             patch('src.main.run_sandbox', return_value=result or self.tests) as sandbox, \
             redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()):
            code = execute(SimpleNamespace(command='check', sha=self.commit.sha), self.settings)
        return code, out.getvalue(), client, judge, sandbox

    def test_clean_and_flagged_flows_keep_tests_separate(self):
        code, output, client, judge, sandbox = self.run_flow()
        self.assertEqual(code, 0)
        self.assertIn('Flag: NO', output)
        self.assertIn('Tests: PASS', output)
        client.tree_paths.assert_called_once_with('a' * 40)
        judge.assert_called_once()
        self.assertEqual(sandbox.call_args.args[1], self.commit.sha)
        flagged = Judgment(flag='yes', reason='Logic without tests in src/a.py with tests/test_a.py.',
                           criteria_hit=['logic_without_tests'])
        code, output, *_ = self.run_flow(judgment=flagged)
        self.assertEqual(code, 1)
        self.assertIn('Tests: PASS', output)
        self.assertIn('Flag: YES', output)

    def test_failed_test_and_timeout_have_different_exit_codes(self):
        for status, expected in [('fail', 1), ('error', 2), ('timeout', 2)]:
            with self.subTest(status=status):
                code, output, *_ = self.run_flow(result=SandboxResult(status, 1, 'evidence', 2))
                self.assertEqual(code, expected)
                self.assertIn(f'Tests: {status.upper()}', output)

    def test_future_baseline_and_already_ingested_commit_are_rejected(self):
        with self.assertRaisesRegex(MemoryError, 'not an ancestor'):
            self.run_flow(ancestor=False)
        self.manifest['shas'].append(self.commit.sha)
        with self.assertRaisesRegex(MemoryError, 'already in the baseline'):
            self.run_flow()

    def test_bad_judgment_does_not_produce_report(self):
        with self.assertRaises(JudgeError):
            self.run_flow(model_error=JudgeError('bad response'))

    def test_missing_key_fails_readably_without_traceback(self):
        with patch('src.main.Settings.load', side_effect=ValueError('Set GROQ_API_KEY')), \
             redirect_stderr(StringIO()) as out:
            self.assertEqual(main(['check', 'b' * 40]), 2)
        self.assertIn('GROQ_API_KEY', out.getvalue())
        self.assertNotIn('Traceback', out.getvalue())

    def test_ingest_uses_requested_baseline_ref(self):
        with patch('src.main.GitHubClient') as github, patch('src.main.Memory') as memory, \
             redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()):
            github.return_value.history.return_value = [self.commit] * 35
            memory.return_value.ingest = AsyncMock(return_value=self.manifest)
            code = execute(SimpleNamespace(command='ingest', count=35, ref='a' * 40), self.settings)
            github.return_value.history.assert_called_once_with(count=35, ref='a' * 40)
        self.assertEqual(code, 0)
        self.assertIn('Baseline ready: 35', out.getvalue())

    def test_report_strips_terminal_controls(self):
        self.assertEqual(terminal_text('\x1b[2Jbad\x1b]0;title\x07\u202e'), 'bad')
        result = SandboxResult('fail', 1, '\x1b[31mFAIL\x1b[0m\r', 2)
        output = render_report('example/repo', self.commit, self.judgment, result, 35, 'a'*40, 'pytest')
        self.assertNotIn('\x1b', output)
        self.assertIn('FAIL', output)

    def test_changed_dependency_without_brightdata_key_still_runs_core_review(self):
        self.commit.files[:] = [ChangedFile('package.json', 'modified', 1, 1)]
        context = {'changes': [{'ecosystem': 'npm', 'package': 'sample', 'before': '1', 'version': '2'}],
                   'candidate_sources': [], 'recalled_context': '', 'warnings': ['Set BRIGHTDATA_API_KEY'],
                   'status': 'not configured', 'memory_status': 'not needed'}
        with patch('src.main.collect_dependency_context', AsyncMock(return_value=context)):
            code, output, _, judge, sandbox = self.run_flow()
        self.assertEqual(code, 0)
        self.assertIn('BRIGHTDATA_API_KEY', output)
        self.assertIn('Tests: PASS', output)
        judge.assert_called_once()
        sandbox.assert_called_once()

    def test_dependency_flow_uses_one_judgment_reports_source_and_remembers_selection(self):
        self.commit.files[:] = [ChangedFile('package.json', 'modified', 1, 1)]
        settings = Settings('example/repo', '', 'test-key', Path('/tmp/memory'),
                            'test-image', 'pytest', 60, 35, 'test-brightdata-key')
        client = Mock()
        client.commit.return_value = self.commit
        client.is_ancestor.return_value = True
        client.diff.return_value = 'full diff'
        client.tree_paths.return_value = ['package.json']
        client.file_text.side_effect = lambda path, sha: json.dumps({'dependencies': {'sample':
                                                                                  '1' if sha == 'a'*40 else '2'}})
        memory = Mock(search=AsyncMock(return_value='baseline'), dependency_context=AsyncMock(return_value=''),
                      remember_dependency_context=AsyncMock())
        memory.manifest.return_value = self.manifest
        brightdata = Mock()
        brightdata.fetch_context.return_value = {'title': 'Sample 2 release',
            'source_url': 'https://github.com/example/sample/releases/tag/v2',
            'text': 'Version 2 removes the legacy interface.'}
        reply = json.dumps({'flag': 'no', 'reason': 'No fixed criterion established.', 'criteria_hit': [],
                           'dependency_notes': [{'source_id': 'source_1', 'summary': 'The legacy interface was removed.',
                                                 'evidence_quote': 'Version 2 removes the legacy interface.'}]})
        response = Mock(stop_reason='end_turn')
        response.__str__ = Mock(return_value=reply)
        agent = Mock(return_value=response)
        with patch('src.main.GitHubClient', return_value=client), patch('src.main.Memory', return_value=memory), \
             patch('src.main.preflight'), patch('src.main.run_sandbox', return_value=self.tests), \
             patch('src.dependency_context.BrightDataClient', return_value=brightdata), \
             patch('src.judge._make_agent', return_value=agent), \
             redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()):
            code = execute(SimpleNamespace(command='check', sha=self.commit.sha), settings)
        self.assertEqual(code, 0)
        agent.assert_called_once()
        self.assertIn('legacy interface was removed', out.getvalue())
        self.assertIn('/releases/tag/v2', out.getvalue())
        self.assertIn('saved separately in Cognee', out.getvalue())
        memory.remember_dependency_context.assert_awaited_once()
        memory.ingest.assert_not_called()
