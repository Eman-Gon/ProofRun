from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.github_fetch import ChangedFile, Commit
from src.memory import Memory, MemoryError


def commit(number=99):
    return Commit(
        sha=f"{number:040x}", author="Ada", date="2026-09-21T00:00:00Z",
        message="Add parser tests", files=[ChangedFile("tests/test_parser.py", "added", 8, 0)],
        additions=8, deletions=0, parents=[],
    )


def dependency_note(**overrides):
    return {
        "ecosystem": "npm", "package": "example-package", "version": "2.0.0",
        "source_url": "https://example.org/releases/2.0.0", "title": "Version 2.0.0 release notes",
        "text": "Version 2.0.0 changes the parser's handling of empty inputs.",
        "fetched_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        **overrides,
    }


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.memory = Memory("example/repo", Path(self.directory.name))
        self.cognee = SimpleNamespace(add=AsyncMock(), cognify=AsyncMock(), search=AsyncMock())
        self.cognee.add.side_effect = lambda data, dataset_name: SimpleNamespace(
            status="PipelineRunCompleted", dataset_name=dataset_name)
        self.cognee.cognify.side_effect = lambda datasets, **kw: {"id": SimpleNamespace(
            status="PipelineRunCompleted", dataset_name=datasets[0])}
        self.search_module = SimpleNamespace(SearchType=SimpleNamespace(CHUNKS="chunks"))
        self.imports = patch("src.memory.importlib.import_module", side_effect=self.import_module)
        self.imported = self.imports.start()
        self.addCleanup(self.imports.stop)
        self.environment = patch.dict(os.environ, {"GROQ_API_KEY": "test-only-key"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.commits = [commit(number) for number in range(30, 0, -1)]

    def import_module(self, name):
        if name == "cognee":
            self.assertEqual(os.environ["LLM_PROVIDER"], "custom")
            self.assertEqual(os.environ["LLM_API_KEY"], "test-only-key")
            self.assertEqual(os.environ["EMBEDDING_PROVIDER"], "fastembed")
            self.assertEqual(os.environ["ENABLE_BACKEND_ACCESS_CONTROL"], "true")
            return self.cognee
        return self.search_module

    async def test_success_stores_each_commit_and_publishes_manifest(self):
        async def confirm_not_yet_published(**kwargs):
            self.assertFalse(self.memory.manifest_path.exists())
            return {"id": SimpleNamespace(status="PipelineRunCompleted", dataset_name=kwargs["datasets"][0])}
        self.cognee.cognify.side_effect = confirm_not_yet_published
        manifest = await self.memory.ingest(self.commits)
        self.assertEqual(self.memory.manifest(), manifest)
        self.assertEqual(manifest["head"], self.commits[0].sha)
        self.cognee.add.assert_awaited_once_with(
            [value.memory_text() for value in self.commits], dataset_name=manifest["dataset"],
        )
        self.cognee.cognify.assert_awaited_once_with(datasets=[manifest["dataset"]], run_in_background=False)

    async def test_failed_reingestion_preserves_previous_baseline(self):
        previous = await self.memory.ingest(self.commits)
        self.cognee.cognify.side_effect = RuntimeError("provider unavailable")
        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            await self.memory.ingest(self.commits)
        self.assertEqual(self.memory.manifest(), previous)
        datasets = [call.kwargs["dataset_name"] for call in self.cognee.add.await_args_list]
        self.assertNotEqual(*datasets)

    async def test_returned_add_error_does_not_publish_partial_baseline(self):
        previous = await self.memory.ingest(self.commits)
        self.cognee.cognify.reset_mock()
        self.cognee.add.side_effect = lambda data, dataset_name: SimpleNamespace(
            status="PipelineRunErrored", dataset_name=dataset_name)
        with self.assertRaisesRegex(MemoryError, "add did not complete"):
            await self.memory.ingest(self.commits)
        self.cognee.cognify.assert_not_awaited()
        self.assertEqual(self.memory.manifest(), previous)

    async def test_empty_or_incomplete_cognify_result_is_not_ready(self):
        previous = await self.memory.ingest(self.commits)
        self.cognee.cognify.side_effect = None
        for result in ({}, None, {"id": SimpleNamespace(status="PipelineRunStarted")}):
            self.cognee.cognify.return_value = result
            with self.assertRaisesRegex(MemoryError, "cognify did not complete"):
                await self.memory.ingest(self.commits)
            self.assertEqual(self.memory.manifest(), previous)

    async def test_search_requires_isolated_nonempty_context(self):
        manifest = await self.memory.ingest(self.commits)
        self.cognee.search.return_value = [
            {"dataset_name": "unrelated", "search_result": "Never use unrelated memory."},
            {"dataset_name": manifest["dataset"], "search_result": " Ada normally updates parser tests. "},
        ]
        self.assertEqual(await self.memory.search(commit()), "Ada normally updates parser tests.")
        arguments = self.cognee.search.await_args.kwargs
        self.assertEqual(arguments["datasets"], [manifest["dataset"]])
        self.assertEqual(arguments["query_type"], "chunks")
        self.assertTrue(arguments["only_context"])
        self.cognee.search.return_value = [{"dataset_name": manifest["dataset"], "search_result": " "}]
        with self.assertRaisesRegex(MemoryError, "no baseline context"):
            await self.memory.search(commit())

    async def test_baseline_commit_cannot_judge_itself(self):
        await self.memory.ingest(self.commits)
        with self.assertRaisesRegex(MemoryError, "already in the baseline"):
            await self.memory.search(self.commits[0])
        self.cognee.search.assert_not_awaited()

    async def test_missing_key_fails_before_ingestion(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": ""}):
            with self.assertRaisesRegex(MemoryError, "GROQ_API_KEY"):
                await self.memory.ingest(self.commits)
        self.cognee.add.assert_not_awaited()

    async def test_manifest_rejects_wrong_repo_and_corruption(self):
        with self.assertRaisesRegex(MemoryError, "No baseline"):
            self.memory.manifest()
        await self.memory.ingest(self.commits)
        other = Memory("another/repo", self.memory.state_dir)
        with self.assertRaisesRegex(MemoryError, "another repo"):
            other.manifest()
        self.memory.manifest_path.write_text("{broken")
        with self.assertRaisesRegex(MemoryError, "Cannot read"):
            self.memory.manifest()

    async def test_dependency_notes_are_sourced_and_separate_from_baseline(self):
        baseline = await self.memory.ingest(self.commits)
        original = self.memory.manifest_path.read_bytes()
        note = dependency_note(judgment={"flag": "yes"}, test_result="failed")
        await self.memory.remember_dependency_context([note])
        added = self.cognee.add.await_args
        self.assertNotEqual(added.kwargs["dataset_name"], baseline["dataset"])
        self.assertTrue(added.kwargs["dataset_name"].startswith(self.memory.dependency_namespace))
        document = json.loads(added.args[0][0].split("\n", 1)[1])
        self.assertEqual(document["source_url"], note["source_url"])
        self.assertEqual(document["version"], "2.0.0")
        self.assertEqual(document["repo"], "example/repo")
        self.assertNotIn("judgment", document)
        self.assertNotIn("test_result", document)
        self.assertEqual(self.memory.manifest_path.read_bytes(), original)

    async def test_dependency_pipeline_returned_errors_preserve_previous_index(self):
        original_note = dependency_note()
        await self.memory.remember_dependency_context([original_note])
        original = self.memory.dependency_index_path.read_bytes()
        updated = dependency_note(fetched_at=datetime.now(timezone.utc).isoformat())
        completed_add = self.cognee.add.side_effect
        self.cognee.add.side_effect = lambda data, dataset_name: SimpleNamespace(
            status="PipelineRunErrored", dataset_name=dataset_name)
        self.cognee.cognify.reset_mock()
        with self.assertRaisesRegex(MemoryError, "dependency add did not complete"):
            await self.memory.remember_dependency_context([updated])
        self.cognee.cognify.assert_not_awaited()
        self.assertEqual(self.memory.dependency_index_path.read_bytes(), original)
        self.cognee.add.side_effect = completed_add
        self.cognee.cognify.side_effect = lambda datasets, **kw: {"id": SimpleNamespace(
            status="PipelineRunErrored", dataset_name=datasets[0])}
        with self.assertRaisesRegex(MemoryError, "dependency cognify did not complete"):
            await self.memory.remember_dependency_context([updated])
        self.assertEqual(self.memory.dependency_index_path.read_bytes(), original)

    async def test_dependency_batch_is_published_only_when_every_note_completes(self):
        notes = [dependency_note(), dependency_note(package="another-package")]
        self.cognee.cognify.side_effect = lambda datasets, **kwargs: {"id": SimpleNamespace(
            status="PipelineRunCompleted" if self.cognee.cognify.await_count == 1 else "PipelineRunErrored",
            dataset_name=datasets[0],
        )}
        with self.assertRaisesRegex(MemoryError, "dependency cognify did not complete"):
            await self.memory.remember_dependency_context(notes)
        self.assertEqual(self.cognee.cognify.await_count, 2)
        self.assertFalse(self.memory.dependency_index_path.exists())

    async def test_missing_dependency_context_does_not_import_cognee(self):
        await self.memory.remember_dependency_context([])
        self.assertEqual(await self.memory.dependency_context([]), "")
        self.assertEqual(await self.memory.dependency_context([dependency_note()]), "")
        self.imported.assert_not_called()

    async def test_dependency_retrieval_filters_version_age_and_future_timestamps(self):
        fresh = dependency_note()
        await self.memory.remember_dependency_context([fresh])
        index = json.loads(self.memory.dependency_index_path.read_text())
        valid = index["entries"][0]
        now = datetime.now(timezone.utc)
        index["entries"].extend([
            {**valid, "version": "3.0.0", "dataset": self.memory.dependency_namespace + "new-version"},
            {**valid, "ecosystem": "pypi", "dataset": self.memory.dependency_namespace + "other-ecosystem"},
            {**valid, "package": "another-package", "dataset": self.memory.dependency_namespace + "other-package"},
            {**valid, "fetched_at": (now - timedelta(days=8)).isoformat(),
             "dataset": self.memory.dependency_namespace + "expired"},
            {**valid, "fetched_at": (now + timedelta(days=1)).isoformat(),
             "dataset": self.memory.dependency_namespace + "future"},
        ])
        self.memory.dependency_index_path.write_text(json.dumps(index))
        self.cognee.search.return_value = [
            {"dataset_name": valid["dataset"], "search_result": "Exact version source excerpt."},
            {"dataset_name": "unrelated", "search_result": "Do not return another dataset."},
        ]
        context = await self.memory.dependency_context([fresh])
        arguments = self.cognee.search.await_args.kwargs
        self.cognee.search.assert_awaited_once()
        self.assertEqual(arguments["datasets"], [valid["dataset"]])
        self.assertEqual(arguments["query_type"], "chunks")
        self.assertTrue(arguments["only_context"])
        self.assertIn("Exact version source excerpt.", context)
        self.assertIn(fresh["source_url"], context)
        self.assertNotIn("Do not return", context)
        self.cognee.search.reset_mock()
        self.imported.reset_mock()
        self.assertEqual(await self.memory.dependency_context([dependency_note(version="9.0.0")]), "")
        self.imported.assert_not_called()

    async def test_dependency_refresh_replaces_exact_source_index_after_success(self):
        first = dependency_note()
        await self.memory.remember_dependency_context([first])
        original = json.loads(self.memory.dependency_index_path.read_text())["entries"][0]
        refreshed = dependency_note(fetched_at=datetime.now(timezone.utc).isoformat(), text="Refreshed source.")
        await self.memory.remember_dependency_context([refreshed])
        entries = json.loads(self.memory.dependency_index_path.read_text())["entries"]
        self.assertEqual(len(entries), 1)
        self.assertNotEqual(entries[0]["dataset"], original["dataset"])
        self.assertEqual(entries[0]["fetched_at"], refreshed["fetched_at"])
        self.cognee.add.reset_mock()
        await self.memory.remember_dependency_context([first])
        self.cognee.add.assert_not_awaited()

    async def test_unsourced_stale_or_future_notes_are_not_stored(self):
        now = datetime.now(timezone.utc)
        for change in ({"source_url": ""}, {"source_url": "http://example.org"}, {"text": " "},
                       {"fetched_at": (now - timedelta(days=8)).isoformat()},
                       {"fetched_at": (now + timedelta(days=1)).isoformat()}):
            with self.subTest(change=change), self.assertRaises(MemoryError):
                await self.memory.remember_dependency_context([dependency_note(**change)])
        self.cognee.add.assert_not_awaited()

    async def test_dependency_index_is_bounded_and_repo_scoped(self):
        now = datetime.now(timezone.utc)
        notes = [dependency_note(
            package=f"package-{number}", fetched_at=(now - timedelta(minutes=number + 1)).isoformat(),
        ) for number in range(101)]
        await self.memory.remember_dependency_context(notes)
        entries = json.loads(self.memory.dependency_index_path.read_text())["entries"]
        self.assertEqual(len(entries), 100)
        self.assertNotIn("package-100", [entry["package"] for entry in entries])
        other = Memory("other/repo", self.memory.state_dir)
        self.assertNotEqual(other.dependency_namespace, self.memory.dependency_namespace)
        self.imported.reset_mock()
        with self.assertRaisesRegex(MemoryError, "another repo"):
            await other.dependency_context(notes)
        self.imported.assert_not_called()


if __name__ == "__main__":
    unittest.main()
