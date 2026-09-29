import unittest
import base64
from unittest.mock import Mock

from src.github_fetch import GitHubClient, GitHubError, validate_sha


def response(data, *, next_page=False):
    result = Mock(ok=True, headers={})
    result.json.return_value = data
    result.links = {"next": {"url": "ignored"}} if next_page else {}
    return result


def commit_data(files=None):
    return {
        "sha": "a" * 40, "author": None,
        "commit": {"message": "Add parser tests", "author": {"name": "Ada", "date": "2026-09-21T00:00:00Z"}},
        "files": files or [], "stats": {"additions": 5, "deletions": 1}, "parents": [],
    }


class GitHubTests(unittest.TestCase):
    def client(self, responses):
        session = Mock(headers={})
        session.get.side_effect = responses
        return GitHubClient("example/repo", session=session)

    def test_paginates_files_and_handles_unlinked_author(self):
        one = {"filename": "src/a.py", "status": "modified", "additions": 4, "deletions": 1, "patch": "secret diff"}
        two = {"filename": "tests/test_a.py", "status": "added", "additions": 1, "deletions": 0}
        client = self.client([response(commit_data([one]), next_page=True), response(commit_data([two]))])
        commit = client.commit("a" * 40)
        self.assertEqual(commit.author, "Ada")
        self.assertEqual(len(commit.files), 2)
        self.assertNotIn("secret diff", commit.memory_text())
        self.assertIn("tests/test_a.py", commit.memory_text())

    def test_short_history_is_not_silently_accepted(self):
        with self.assertRaisesRegex(GitHubError, "fewer than 30"):
            self.client([response([])]).history()

    def test_rate_limit_has_actionable_error(self):
        result = Mock(ok=False, status_code=403, headers={"X-RateLimit-Remaining": "0"})
        with self.assertRaisesRegex(GitHubError, "rate limit"):
            self.client([result]).commit("a" * 40)

    def test_oversized_diff_fails_instead_of_truncating(self):
        result = response({})
        result.content = b"x" * 120001
        with self.assertRaisesRegex(GitHubError, "instead of truncating"):
            self.client([result]).diff("a" * 40)

    def test_tree_truncation_fails(self):
        with self.assertRaisesRegex(GitHubError, "truncated"):
            self.client([response({"truncated": True})]).tree_paths("a" * 40)

    def test_dependency_snapshot_uses_exact_commit_and_decodes_complete_file(self):
        data = {'type': 'file', 'encoding': 'base64', 'size': 4,
                'content': base64.b64encode(b'text').decode()}
        client = self.client([response(data)])
        self.assertEqual(client.file_text('nested/package.json', 'a' * 40), 'text')
        call = client.session.get.call_args
        self.assertTrue(call.args[0].endswith('/contents/nested/package.json'))
        self.assertEqual(call.kwargs['params'], {'ref': 'a' * 40})

    def test_incomplete_or_oversized_dependency_snapshot_is_rejected(self):
        for data in [{'type': 'dir'}, {'type': 'file', 'encoding': 'none'},
                     {'type': 'file', 'encoding': 'base64', 'size': 300000},
                     {'type': 'file', 'encoding': 'base64', 'content': 'invalid!'}]:
            with self.assertRaises(GitHubError):
                self.client([response(data)]).file_text('package.json', 'a' * 40)

    def test_sha_rejects_options_and_shell_fragments(self):
        for sha in ["--help", "main", "abc1234;id", "a" * 41]:
            with self.assertRaises(ValueError):
                validate_sha(sha)


if __name__ == "__main__":
    unittest.main()
