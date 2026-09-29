import json
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.brightdata import BrightDataError
from src.dependency_context import collect_dependency_context, remember_selected_notes
from src.github_fetch import ChangedFile, Commit, GitHubError
from src.judge import Judgment


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.github = Mock()
        self.memory = Mock(dependency_context=AsyncMock(return_value=''),
                           remember_dependency_context=AsyncMock())
        self.commit = Commit('b'*40, 'Ada', '2026-09-21', 'Upgrade',
                             [ChangedFile('package.json', 'modified', 1, 1)], 1, 1, ['a'*40])
        self.before = json.dumps({'dependencies': {'sample': '1.0'}})
        self.after = json.dumps({'dependencies': {'sample': '2.0'}})
        self.github.file_text.side_effect = lambda path, sha: self.before if sha == 'a'*40 else self.after
        self.client = Mock()
        self.client.fetch_context.return_value = {'source_url': 'https://github.com/example/sample/releases/tag/v2',
            'title': 'Sample 2.0', 'text': 'Version 2.0 requires Python 3.12 or newer.'}
        self.factory_patch = patch('src.dependency_context.BrightDataClient', return_value=self.client)
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)

    async def collect(self, key='fake-key'):
        return await collect_dependency_context(self.github, self.commit, self.memory, key)

    async def test_no_dependency_or_config_only_changes_make_no_web_or_memory_calls(self):
        self.commit.files[:] = [ChangedFile('src/a.py', 'modified', 1, 1)]
        self.assertIsNone(await self.collect())
        self.github.file_text.assert_not_called()
        self.commit.files[:] = [ChangedFile('package.json', 'modified', 1, 1)]
        self.after = json.dumps({'scripts': {'test': 'new-command'}, 'dependencies': {'sample': '1.0'}})
        self.assertEqual((await self.collect())['status'], 'unchanged')
        self.factory.assert_not_called()
        self.memory.dependency_context.assert_not_awaited()

    async def test_changed_package_retrieves_source_and_recalled_notes(self):
        self.memory.dependency_context.return_value = 'Dated prior note'
        context = await self.collect()
        self.client.fetch_context.assert_called_once_with(package='sample', ecosystem='npm', version='2.0')
        self.assertEqual(context['recalled_context'], 'Dated prior note')
        self.assertEqual(context['candidate_sources'][0]['source_id'], 'source_1')
        self.assertIn('fetched_at', context['candidate_sources'][0])
        self.assertEqual(context['changes'][0]['before'], '1.0')

    async def test_missing_key_failure_and_unsupported_file_are_visible(self):
        context = await self.collect(key='')
        self.assertEqual(context['status'], 'not configured')
        self.factory.assert_not_called()
        self.client.fetch_context.side_effect = BrightDataError('unavailable')
        context = await self.collect()
        self.assertEqual(context['status'], 'unavailable')
        self.assertTrue(context['warnings'])
        self.commit.files[:] = [ChangedFile('pnpm-lock.yaml', 'modified', 1, 1)]
        self.assertEqual((await self.collect())['status'], 'incomplete')

    async def test_missing_snapshot_does_not_claim_no_changes(self):
        self.github.file_text.side_effect = GitHubError('missing snapshot')
        context = await self.collect()
        self.assertEqual(context['status'], 'incomplete')
        self.factory.assert_not_called()

    async def test_removed_packages_need_no_network_or_recall(self):
        self.after = '{}'
        context = await self.collect()
        self.assertEqual(context['status'], 'removals only')
        self.factory.assert_not_called()
        self.memory.dependency_context.assert_not_awaited()

    async def test_queries_capped_and_direct_changes_preferred(self):
        self.before = '{}'
        self.after = json.dumps({'dependencies': {f'package{i}': '1.0' for i in range(6)}})
        context = await self.collect()
        self.assertEqual(self.client.fetch_context.call_count, 3)
        self.assertEqual(len(context['changes']), 6)
        self.assertTrue(any('3 of 6' in text for text in context['warnings']))

    async def test_only_selected_notes_are_stored_and_failure_is_nonfatal(self):
        context = await self.collect()
        judgment = Judgment(flag='no', reason='No criterion established.', criteria_hit=[], dependency_notes=[
            {'source_id': 'source_1', 'summary': 'Python support changed.',
             'evidence_quote': 'Version 2.0 requires Python 3.12 or newer.'}])
        await remember_selected_notes(self.memory, context, judgment)
        stored = self.memory.remember_dependency_context.await_args.args[0]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['version'], '2.0')
        self.assertIn('Python support changed.', stored[0]['text'])
        self.assertNotIn('flag', stored[0])
        self.memory.remember_dependency_context.side_effect = RuntimeError('provider failure')
        await remember_selected_notes(self.memory, context, judgment)
        self.assertEqual(context['memory_status'], 'not saved')
        self.assertTrue(any('could not be saved' in text for text in context['warnings']))

    async def test_same_format_rename_without_version_change_skips_lookup(self):
        self.commit.files[:] = [ChangedFile('nested/package.json', 'renamed', 0, 0, 'package.json')]
        self.after = self.before
        self.assertEqual((await self.collect())['status'], 'unchanged')
        self.factory.assert_not_called()

    async def test_slow_optional_memory_operations_have_deadlines(self):
        async def delayed(*args):
            await asyncio.sleep(10)
        self.memory.dependency_context.side_effect = delayed
        with patch('src.dependency_context.MEMORY_TIMEOUT', 0.01):
            context = await self.collect()
        self.assertTrue(any('could not be recalled' in text for text in context['warnings']))
        judgment = Judgment(flag='no', reason='No criterion established.', criteria_hit=[], dependency_notes=[
            {'source_id': 'source_1', 'summary': 'Python support changed.',
             'evidence_quote': 'Version 2.0 requires Python 3.12 or newer.'}])
        self.memory.remember_dependency_context.side_effect = delayed
        with patch('src.dependency_context.MEMORY_TIMEOUT', 0.01):
            await remember_selected_notes(self.memory, context, judgment)
        self.assertEqual(context['memory_status'], 'not saved')
